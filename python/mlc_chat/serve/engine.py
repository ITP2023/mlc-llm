"""The MLC LLM Serving Engine."""
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import tvm
from tvm.runtime import Device

from mlc_chat.serve import data
from mlc_chat.support.auto_device import detect_device

from ..chat_module import _get_chat_config, _get_lib_module_path, _get_model_path
from ..streamer import StopStringHandler, TextStreamer
from ..tokenizer import Tokenizer
from . import data
from .config import EngineMode, GenerationConfig, KVCacheConfig
from .event_trace_recorder import EventTraceRecorder
from .request import Request, RequestStreamOutput


@dataclass
class ModelInfo:
    """The model info dataclass.

    Parameters
    ----------
    model : str
        The identifier of the input model.
        It may be a compiled model's id (e.g., "Llama-2-7b-chat-hf-q4f16_1"),
        or a full path to a model directory
        (e.g., "dist/prebuilt/mlc-chat-Llama-2-7b-chat-hf-q4f16_1")

    device : str
        The device where to run the model.
        It can be "auto", "device_name" (e.g., "cuda") or
        "device_name:device_id" (e.g., "cuda:1").

    model_lib_path : str
        The path to the compiled library of the model.
        E.g., "dist/prebuilt/lib/Llama-2-7b-chat-hf-q4f16_1-cuda.so"
    """

    model: str
    model_lib_path: str
    device: Device = "auto"  # type: ignore

    def __post_init__(self):
        if isinstance(self.device, str):
            self.device = detect_device(self.device)
        assert isinstance(self.device, Device)


def _create_tvm_module(
    creator: str, ffi_funcs: Sequence[str], creator_args: Optional[List[Any]] = None
) -> Dict[str, Callable]:
    """Internal method to create a module."""
    if creator_args is None:
        creator_args = []
    module = tvm.get_global_func(creator, allow_missing=False)(*creator_args)
    return {key: module[key] for key in ffi_funcs}


def _process_model_args(
    models: Union[ModelInfo, List[ModelInfo]]
) -> Tuple[List[Any], str, int, Optional[str]]:
    """Process the input ModelInfo to get the engine initialization arguments."""
    max_single_sequence_length = int(1e9)
    tokenizer_path: Optional[str] = None
    conv_template_name: Optional[str] = None

    def _convert_model_info(model: ModelInfo) -> List[Any]:
        nonlocal max_single_sequence_length, tokenizer_path, conv_template_name

        device = model.device
        model_path, config_file_path = _get_model_path(model.model)
        chat_config = _get_chat_config(config_file_path, user_chat_config=None)
        if chat_config.context_window_size:
            max_single_sequence_length = min(
                max_single_sequence_length,
                chat_config.context_window_size,
            )
        if tokenizer_path is None:
            tokenizer_path = model_path
        if conv_template_name is None:
            conv_template_name = chat_config.conv_template
        # Try look up model library, and do JIT compile if model library not found.
        try:
            model_lib_path = _get_lib_module_path(
                model=model.model,
                model_path=model_path,
                chat_config=chat_config,
                model_lib_path=model.model_lib_path,
                device_name=device.MASK2STR[device.device_type],
                config_file_path=config_file_path,
            )
        except FileNotFoundError:
            from mlc_chat.interface import (  # pylint: disable=import-outside-toplevel
                jit,
            )

            model_lib_path = str(
                jit.jit(
                    model_path=Path(model_path),
                    chat_config=asdict(chat_config),
                    device=device,
                )
            )
        return [model_lib_path, model_path, device.device_type, device.device_id]

    if isinstance(models, list):
        model_args: List[Any] = sum(
            (_convert_model_info(model) for model in models),
            start=[],
        )
    else:
        model_args = _convert_model_info(models)

    return model_args, tokenizer_path, max_single_sequence_length, conv_template_name


class Engine:
    """The Python interface of request serving engine for MLC LLM.

    The engine can run one or multiple LLM models internally for
    text generation. Usually, when there are multiple models,
    speculative inference will be activated, where the first model
    (index 0) is the main "large model" that has better generation
    quality, and all other models are "small" models that used for
    speculation.

    The engine receives requests from the "add_request" method. For
    an given request, the engine will keep generating new tokens for
    the request until finish (under certain criterion). After finish,
    the engine will return the generation result through the callback
    function provided by the request.

    Parameters
    ----------
    models : Union[ModelInfo, List[ModelInfo]]
        One or a list of model info (specifying which models to load and
        which device to load to) to launch the engine.

    kv_cache_config : KVCacheConfig
        The configuration of the paged KV cache.

    request_stream_callback : Optional[Callable[[str, data.TokenData, Optional[str]], None]]
        The provided callback function to handle the generation
        output. It has the signature of `(str, data.TokenData, bool) -> None`,
        where
        - the first string is the request id,
        - the TokenData contains the generated **delta** token ids since
        the last invocation of the callback on the specific request,
        - the optional string value denotes the finish reason if the
        generation of the request is finished, or None if it has not finished.

        The callback function is optional at construction, but it needs to
        be set before the engine executing requests. This can be done via
        the `set_request_stream_callback` method. Otherwise, the engine will raise
        exception.

    engine_mode : Optional[EngineMode]
        The Engine execution mode.

    enable_tracing : bool
        A boolean indicating if to enable event logging for requests.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        models: Union[ModelInfo, List[ModelInfo]],
        kv_cache_config: KVCacheConfig,
        engine_mode: Optional[EngineMode] = None,
        request_stream_callback: Optional[Callable[[List[RequestStreamOutput]], None]] = None,
        enable_tracing: bool = False,
    ):
        (
            model_args,
            tokenizer_path,
            self.max_single_sequence_length,
            self.conv_template_name,
        ) = _process_model_args(models)
        self._ffi = _create_tvm_module(
            "mlc.serve.create_engine",
            ffi_funcs=[
                "init",
                "add_request",
                "abort_request",
                "step",
                "stats",
                "reset",
                "get_request_stream_callback",
                "set_request_stream_callback",
            ],
        )
        self.trace_recorder = EventTraceRecorder() if enable_tracing else None

        if engine_mode is None:
            # The default engine mode: non-speculative
            engine_mode = EngineMode()

        self._ffi["init"](
            self.max_single_sequence_length,
            tokenizer_path,
            kv_cache_config.asjson(),
            engine_mode.asjson(),
            request_stream_callback,
            self.trace_recorder,
            *model_args,
        )
        self.tokenizer = Tokenizer(tokenizer_path)

    def generate(
        self,
        prompts: Union[str, List[str], List[int], List[List[int]]],
        generation_config: Union[GenerationConfig, List[GenerationConfig]],
    ) -> List[str]:
        """Generate texts for a list of input prompts.
        Each prompt can be a string or a list of token ids.
        The generation for each prompt is independent.
        Return the generation results, one for each prompt.

        Parameters
        ----------
        prompts : Union[str, List[str], List[int], List[List[int]]]
            One or a list of input prompts for text generation.
            Each prompt can be a string or a list of token ids.

        generation_config : Union[GenerationConfig, List[GenerationConfig]]
            The generation config for each requests.
            If the it is a single GenerationConfig instance,
            this config will be shared by all the prompts.
            Otherwise, one generation config is required for every
            prompt.

        Returns
        -------
        results : List[str]
            The text generation results, one string for each input prompt.
        """
        if isinstance(prompts, str):
            # `prompts` is a single string.
            prompts = [prompts]
        else:
            assert isinstance(prompts, list), (
                "Input `prompts` is expected to be a string, a list of "
                "str, a list of token ids or multiple lists of token ids."
            )
            if len(prompts) == 0:
                return []
            if isinstance(prompts[0], int):
                # `prompts` is a list of token ids
                prompts = [prompts]  # type: ignore

        num_requests = len(prompts)
        if not isinstance(generation_config, list):
            generation_config = [generation_config] * num_requests

        assert (
            len(generation_config) == num_requests
        ), "Number of generation config and number of prompts mismatch"

        num_finished_requests = 0
        outputs: List[str] = []
        text_streamers: List[TextStreamer] = []
        stop_handlers: List[StopStringHandler] = []
        for i in range(num_requests):
            outputs.append("")
            text_streamers.append(TextStreamer(self.tokenizer))
            stop_handlers.append(StopStringHandler(generation_config[i].stop_strs))

        # Save a copy of the original function callback since `generate`
        # overrides the callback function.
        # The original callback will be set back later on.
        original_callback = self._ffi["get_request_stream_callback"]()

        # Define the callback function for request generation results
        def request_stream_callback(delta_outputs: List[RequestStreamOutput]):
            nonlocal num_finished_requests
            for delta_output in delta_outputs:
                request_id, delta_tokens, finish_reason = delta_output.unpack()
                rid = int(request_id)
                text_streamer = text_streamers[rid]
                stop_handler = stop_handlers[rid]

                delta_text = stop_handler.put(text_streamer.put(delta_tokens.token_ids))
                if stop_handler.stop_triggered:
                    finish_reason = "stop"
                elif finish_reason is not None:
                    delta_text += stop_handler.put(text_streamer.finish())
                    if stop_handler.stop_triggered:
                        finish_reason = "stop"
                    else:
                        delta_text += stop_handler.finish()

                outputs[rid] += delta_text
                if finish_reason is not None:
                    num_finished_requests += 1

        # Override the callback function in engine.
        self._ffi["set_request_stream_callback"](request_stream_callback)

        # Add requests to engine.
        for req_id, (prompt, generation_cfg) in enumerate(zip(prompts, generation_config)):
            input_data = (
                data.TextData(prompt)
                if isinstance(prompt, str)
                else data.TokenData(prompt)  # type: ignore
            )
            self.add_request(
                Request(
                    request_id=str(req_id),
                    inputs=input_data,
                    generation_config=generation_cfg,
                )
            )

        while num_finished_requests != num_requests:
            self.step()

        # Restore the callback function in engine.
        self._ffi["set_request_stream_callback"](original_callback)
        return outputs

    def add_request(self, request: Request) -> None:
        """Add a new request to the engine.

        Parameters
        ----------
        request : Request
            The request to add.
        """
        self._ffi["add_request"](request)

    def abort_request(self, request_id: str) -> None:
        """Abort the generation of the request corresponding to the input request id.

        Parameters
        ----------
        request_id : str
            The unique id of the request to abort.
        """
        self._ffi["abort_request"](request_id)

    def step(self) -> None:
        """The main function that the engine takes a step of action.

        At each step, the engine may decide to
        - run prefill for one (or more) requests,
        - run one-step decode for the all existing requests
        ...

        In the end of certain actions (e.g., decode), the engine will
        check if any request has finished, and will return the
        generation results for those finished requests.
        """
        self._ffi["step"]()

    def reset(self) -> None:
        """Reset the engine, clean up all running data and statistics."""
        self._ffi["reset"]()

    def stats(self) -> Dict[str, float]:
        """The engine runtime statistics.
        We collect the following entries:
        - single token prefill latency (s/tok): avg latency of processing one token in prefill
        - single token decode latency (s/tok): avg latency of processing one token in decode
        - engine time for prefill (sec)
        - engine time for decode (sec)
        - total number of processed tokens in prefill.
        - total number of processed tokens in decode.
        """
        stats_json_str = self._ffi["stats"]()
        return json.loads(stats_json_str)
