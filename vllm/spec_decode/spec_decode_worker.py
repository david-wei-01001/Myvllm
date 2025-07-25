# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import copy
from collections import defaultdict
from functools import cached_property
from typing import Any, Dict, List, Optional, Set, Tuple, Type

import torch
import torch.nn as nn

from vllm.config import ParallelConfig, SpeculativeConfig, VllmConfig
from vllm.distributed.communication_op import (broadcast_tensor_dict,
                                               get_tp_group,
                                               tensor_model_parallel_gather)
from vllm.distributed.parallel_state import model_parallel_is_initialized
from vllm.logger import init_logger
from vllm.model_executor.layers.rejection_sampler import RejectionSampler
from vllm.model_executor.layers.sampler import SamplerOutput
from vllm.model_executor.layers.spec_decode_base_sampler import (
    SpecDecodeBaseSampler, SpecDecodeStochasticBaseSampler)
from vllm.model_executor.layers.typical_acceptance_sampler import (
    TypicalAcceptanceSampler)
from vllm.platforms import current_platform
from vllm.sequence import (VLLM_INVALID_TOKEN_ID,
                           CompletionSequenceGroupOutput, ExecuteModelRequest,
                           HiddenStates, SequenceGroupMetadata,
                           get_all_seq_ids_and_request_ids)
from vllm.spec_decode.batch_expansion import BatchExpansionTop1Scorer

if current_platform.is_cuda_alike():
    from vllm.spec_decode.draft_model_runner import TP1DraftModelRunner

from vllm.spec_decode.interfaces import (SpeculativeProposals,
                                         SpeculativeScorer, SpeculativeScores)
from vllm.spec_decode.medusa_worker import MedusaWorker
from vllm.spec_decode.metrics import AsyncMetricsCollector
from vllm.spec_decode.mlp_speculator_worker import MLPSpeculatorWorker
from vllm.spec_decode.mqa_scorer import MQAScorer
from vllm.spec_decode.multi_step_worker import MultiStepWorker
from vllm.spec_decode.ngram_worker import NGramWorker
from vllm.spec_decode.proposer_worker_base import ProposerWorkerBase
from vllm.spec_decode.smaller_tp_proposer_worker import SmallerTpProposerWorker
from vllm.spec_decode.target_model_runner import TargetModelRunner
from vllm.spec_decode.util import (Timer, create_logprobs_output,
                                   create_sequence_group_output,
                                   get_all_num_logprobs,
                                   get_sampled_token_logprobs, nvtx_range,
                                   split_batch_by_proposal_len)
from vllm.utils import resolve_obj_by_qualname
from vllm.worker.worker_base import LoRANotSupportedWorkerBase, WorkerBase

logger = init_logger(__name__)


def create_spec_worker(*args, **kwargs) -> "SpecDecodeWorker":
    """Helper method that is the entrypoint for Executors which use
    WorkerWrapper. It constructs a SpecDecodeWorker from the speculative config.
    """
    vllm_config: VllmConfig = kwargs.get("vllm_config")
    speculative_config: SpeculativeConfig = vllm_config.speculative_config
    assert speculative_config is not None

    if vllm_config.parallel_config.pipeline_parallel_size > 1:
        raise NotImplementedError("Speculative decoding is currently "
                                  "incompatible with pipeline parallelism")

    draft_worker_kwargs = kwargs.copy()

    kwargs["model_runner_cls"] = TargetModelRunner
    target_worker_config = copy.deepcopy(vllm_config)
    target_worker_config.parallel_config.worker_cls =\
        target_worker_config.parallel_config.sd_worker_cls
    logger.info(target_worker_config.parallel_config.worker_cls)
    cls = resolve_obj_by_qualname(
        target_worker_config.parallel_config.worker_cls)
    target_worker = cls(*args, **kwargs)
    # Set the disable_logprobs variable in the TargetModelRunner instance
    # as per its value specified in the SpeculativeConfig.
    target_worker.model_runner.disable_logprobs =\
         speculative_config.disable_logprobs

    draft_worker_config = copy.deepcopy(vllm_config)
    draft_worker_config.model_config = speculative_config.draft_model_config
    draft_worker_config.quant_config = VllmConfig._get_quantization_config(
        draft_worker_config.model_config,
        vllm_config.load_config,
    )
    speculative_config.draft_parallel_config.worker_cls =\
        draft_worker_config.parallel_config.sd_worker_cls
    draft_worker_config.parallel_config = speculative_config.draft_parallel_config  # noqa
    # TODO allow draft-model specific load config.

    # Override draft-model specific worker args.
    draft_worker_kwargs.update(
        vllm_config=draft_worker_config,
        ngram_prompt_lookup_max=speculative_config.prompt_lookup_max,
        ngram_prompt_lookup_min=speculative_config.prompt_lookup_min,
    )

    spec_decode_worker = SpecDecodeWorker.create_worker(
        scorer_worker=target_worker,
        draft_worker_kwargs=draft_worker_kwargs,
        disable_mqa_scorer=speculative_config.disable_mqa_scorer,
        disable_by_batch_size=speculative_config.disable_by_batch_size,
        draft_token_acceptance_method=speculative_config.acceptance_method,
        typical_acceptance_sampler_posterior_threshold=speculative_config.
        posterior_threshold,
        typical_acceptance_sampler_posterior_alpha=speculative_config.
        posterior_alpha,
        disable_logprobs=speculative_config.disable_logprobs,
        disable_log_stats=speculative_config.disable_log_stats,
        num_speculative_tokens=speculative_config.num_speculative_tokens,
    )

    return spec_decode_worker


# Reminder: Please update docs/features/compatibility_matrix.md
# If the feature combo become valid
class SpecDecodeWorker(LoRANotSupportedWorkerBase):
    """Worker which implements speculative decoding.

    Speculative decoding reduces decoding per-token latency by using a proposal
    method, such as a small draft model, to speculate ahead of a larger LLM. The
    probabilities of the speculative tokens are then determined by the larger
    LLM, after which some verification routine determines which (if any) of the
    speculative tokens are accepted by the larger LLM.

    See https://github.com/vllm-project/vllm/pull/2188 and
    https://github.com/vllm-project/vllm/pull/3103 for more info.

    The current implementation has the following limitations:
    * Only draft-model proposal is implemented (contributions for more forms are
        welcome!).
    * Only top-1 proposal and scoring are implemented. Tree-attention is left as
        future work.
    * All sequences in a batch must have the same proposal length, or zero. This
        can be improved by having per-sequence speculation in the future.
    * The scoring forward pass is done without an MQA kernel, which is
        suboptimal especially as the batch size, proposal length, and sequence
        lengths grow. Contributions to add a MQA scoring are welcome once
        correctness tests pass.
        More info here https://docs.google.com/document/d/1T-JaS2T1NRfdP51qzqpyakoCXxSXTtORppiwaj5asxA/edit.
    """

    @classmethod
    def create_worker(
        cls,
        scorer_worker: WorkerBase,
        draft_worker_kwargs: Dict[str, Any],
        disable_mqa_scorer: bool,
        disable_by_batch_size: Optional[int],
        draft_token_acceptance_method: str,
        typical_acceptance_sampler_posterior_threshold: float,
        typical_acceptance_sampler_posterior_alpha: float,
        disable_logprobs: bool,
        disable_log_stats: bool,
        num_speculative_tokens: int,
    ) -> "SpecDecodeWorker":

        allow_zero_draft_token_step = True
        enable_lm_head_weight_load = False
        num_spec_prefill_steps = 1
        ngram_prompt_lookup_max = (
            draft_worker_kwargs.pop("ngram_prompt_lookup_max"))
        ngram_prompt_lookup_min = (
            draft_worker_kwargs.pop("ngram_prompt_lookup_min"))
        draft_model_config = draft_worker_kwargs["vllm_config"].model_config
        draft_parallel_config: ParallelConfig = draft_worker_kwargs[
            'vllm_config'].parallel_config
        if ngram_prompt_lookup_max > 0:
            draft_worker_kwargs[
                "device_type"] = scorer_worker.device_config.device.type
            proposer_worker = NGramWorker(**draft_worker_kwargs)
            proposer_worker.set_ngram_window_size(ngram_prompt_lookup_min,
                                                  ngram_prompt_lookup_max)
        else:
            draft_tp = draft_parallel_config.tensor_parallel_size
            target_tp = scorer_worker.parallel_config.tensor_parallel_size

            if draft_model_config.hf_config.model_type == "mlp_speculator":
                proposer_worker = MLPSpeculatorWorker(**draft_worker_kwargs)
            elif draft_model_config.hf_config.model_type == "medusa":
                proposer_worker = MedusaWorker(**draft_worker_kwargs)
            else:
                if draft_tp == 1:
                    if current_platform.is_cuda_alike():
                        draft_worker_kwargs[
                            "model_runner_cls"] = TP1DraftModelRunner
                else:
                    if draft_model_config.hf_config.model_type == "eagle":
                        raise NotImplementedError(
                            f"{draft_model_config.hf_config.model_type} "
                            "does not support TP > 1 yet")

                    allow_zero_draft_token_step = False

                # Load lm_head weight for eagle in init_device
                if draft_model_config.hf_config.model_type == "eagle":
                    enable_lm_head_weight_load = True

                proposer_worker = MultiStepWorker(**draft_worker_kwargs)
                if draft_model_config.hf_config.model_type == "deepseek_mtp":
                    num_spec_prefill_steps = \
                        draft_model_config.hf_config.n_predict

            proposer_worker = SmallerTpProposerWorker.maybe_wrap_worker(
                proposer_worker, draft_tp, target_tp)

        logger.info("Configuring SpecDecodeWorker with proposer=%s",
                    type(proposer_worker))

        spec_decode_sampler: SpecDecodeBaseSampler = None
        if draft_token_acceptance_method == "rejection_sampler":
            spec_decode_sampler = RejectionSampler()
        elif draft_token_acceptance_method == "typical_acceptance_sampler":
            spec_decode_sampler = TypicalAcceptanceSampler(
                posterior_threshold=\
                    typical_acceptance_sampler_posterior_threshold,
                posterior_alpha=typical_acceptance_sampler_posterior_alpha,
            )
        logger.info(
            "[Speculative Decoding] Configuring"
            " SpecDecodeWorker with sampler=%s", type(spec_decode_sampler))

        if not disable_mqa_scorer:
            if scorer_worker.model_runner.attn_backend.get_name(
            ) != "FLASH_ATTN":
                disable_mqa_scorer = True
                logger.info(
                    "[Speculative Decoding] Disabling MQA scorer as the "
                    "MQA is only available with flash attn backend.")

            if draft_model_config and \
                draft_model_config.max_model_len < \
                    scorer_worker.model_config.max_model_len:
                disable_mqa_scorer = True
                logger.info(
                    "[Speculative Decoding] Disabling MQA scorer as the "
                    "draft model max_model_len is smaller than the target "
                    "model max_model_len.")

            if not scorer_worker.model_runner.model_config.enforce_eager:
                disable_mqa_scorer = True
                logger.info(
                    "[Speculative Decoding] Disabling MQA scorer as the "
                    "target model is not running in eager mode.")

        return SpecDecodeWorker(
            proposer_worker,
            scorer_worker,
            disable_mqa_scorer=disable_mqa_scorer,
            disable_logprobs=disable_logprobs,
            disable_log_stats=disable_log_stats,
            disable_by_batch_size=disable_by_batch_size,
            spec_decode_sampler=spec_decode_sampler,
            allow_zero_draft_token_step=allow_zero_draft_token_step,
            enable_lm_head_weight_load=enable_lm_head_weight_load,
            num_spec_prefill_steps=num_spec_prefill_steps)

    def __init__(
        self,
        proposer_worker: ProposerWorkerBase,
        scorer_worker: WorkerBase,
        spec_decode_sampler: SpecDecodeBaseSampler,
        disable_mqa_scorer: bool = False,
        disable_logprobs: bool = False,
        disable_log_stats: bool = False,
        metrics_collector: Optional[AsyncMetricsCollector] = None,
        disable_by_batch_size: Optional[int] = None,
        allow_zero_draft_token_step: Optional[bool] = True,
        enable_lm_head_weight_load: Optional[bool] = False,
        num_spec_prefill_steps: int = 1,
    ):
        """
        Create a SpecDecodeWorker.

        Args:
            proposer_worker: A worker that can produce speculative tokens for
                sequences.
            scorer_worker: A worker that produces probabilities of speculative
                tokens according to some base model. Typically a vanilla vLLM
                Worker.
            spec_decode_sampler: A Torch module used to perform acceptance
                sampling of the draft tokens in the verification step of
                speculative decoding. Currently we support two different 
                types of sampler namely RejectionSampler and
                TypicalAcceptanceSampler. 'spec_decode_sampler' is either an
                instance of RejectionSampler or TypicalAcceptanceSampler.
            disable_mqa_scorer: If set to True, disable the MQA scorer and use
                the BatchExpansionTop1Scorer instead.
            disable_logprobs: If set to True, token log probabilities will
                not be output in both the draft worker and the target worker.
                If set to False, log probabilities will be output by both.
            disable_log_stats: If set to True, disable periodic printing of
                speculative stage times.
            disable_by_batch_size: If the batch size is larger than this,
                disable speculative decoding for new incoming requests.
            metrics_collector: Helper class for collecting metrics; can be set
                for testing purposes.
            allow_zero_draft_token_step: whether to allow a step where the draft
                model generates no draft token; should disallow when the tp of
                draft model is larger than 1 (TODO: #5814)
            enable_lm_head_weight_load: whether to load lm_head weight for
                draft models like eagle.
            num_spec_prefill_steps: number of speculative prefill steps to run
                before the speculative decoding starts. This is only used when
                the draft model is a deepseek_mtp model that requires prefill
                kv cache separately for each MTP layer.
        """
        self.proposer_worker = proposer_worker
        self.scorer_worker = scorer_worker
        scorer_runner = getattr(self.scorer_worker, "model_runner", None)
        logger.info(scorer_runner)
        self.generators = scorer_runner.get_generators(
        ) if scorer_runner else None
        self.disable_by_batch_size = disable_by_batch_size or float("inf")
        self.spec_decode_sampler = spec_decode_sampler
        self._allow_zero_draft_token_step = allow_zero_draft_token_step
        self._enable_lm_head_weight_load = enable_lm_head_weight_load
        self._metrics = AsyncMetricsCollector(
            self.spec_decode_sampler
        ) if metrics_collector is None else metrics_collector
        # Tracks the sequence IDs that received a bonus token ID in
        # their last forward pass. Needed only if KV cache is being
        # used for token generation such as in the case of MultiStepWorker.
        self._seq_with_bonus_token_in_last_step: Set[int] = set()
        # Tracks the currently active request ids and the sequence IDs
        # corresponding to them
        self._request_id_seq_id_mapping: Dict[str, Set[int]] = defaultdict(set)
        # Tracks if the proposer worker uses the KV cache or not.

        self.probs_dtype = self.spec_decode_sampler.probs_dtype
        self.token_id_dtype = self.spec_decode_sampler.token_id_dtype
        # Lazy initialization.
        self.scorer: SpeculativeScorer
        self.disable_mqa_scorer = disable_mqa_scorer

        # Hidden states from target model to pass to proposer
        # in the subsequent step.
        self.previous_hidden_states: Optional[HiddenStates] = None
        self._disable_logprobs = disable_logprobs
        self._disable_log_stats = disable_log_stats
        self._num_spec_prefill_steps = num_spec_prefill_steps

    def init_device(self) -> None:
        """Initialize both scorer and proposer models.
        """
        # The scorer worker model is initialized first in case the proposer
        # model has a smaller TP degree than the target worker.
        self.scorer_worker.init_device()
        self.proposer_worker.init_device()

        # NOTE(cade): load_model is not part of the WorkerBase interface.
        self.scorer_worker.load_model()
        self.proposer_worker.load_model()

        if self._enable_lm_head_weight_load:
            # NOTE(Shangming): gather lm_head weight when tp enabled
            target_lm_head_weight: torch.Tensor = tensor_model_parallel_gather(
                self.scorer_worker.model_runner.model_runner.model.lm_head.\
                    weight.data,
                    dim=0,
            )

            self.proposer_worker.maybe_load_lm_head_weight(
                target_lm_head_weight)

        self._metrics.init_tensors(self.rank, device_type=self.device)
        if model_parallel_is_initialized():
            self.spec_decode_sampler.init_tensors(get_tp_group().local_rank,
                                                  device_type=self.device)
        else:
            self.spec_decode_sampler.init_tensors(self.rank,
                                                  device_type=self.device)

        scorer_cls: Type[SpeculativeScorer]
        if self.disable_mqa_scorer:
            scorer_cls = BatchExpansionTop1Scorer
            logger.info("[Speculative Decoding] Use batch "
                        "expansion for scoring proposals.")
        else:
            scorer_cls = MQAScorer
            logger.info(
                "[Speculative Decoding] Use MQA scorer for scoring proposals.")

        self.scorer = scorer_cls(scorer_worker=self.scorer_worker,
                                 device=self.device,
                                 vocab_size=self._vocab_size)

        self._configure_model_sampler_for_spec_decode()

    def load_model(self, *args, **kwargs):
        pass

    def _configure_model_sampler_for_spec_decode(self):
        """Configure model sampler to emit GPU tensors. This allows spec decode
        to keep data on device without transferring to CPU and serializing,
        which significantly reduces overhead of sampling during verification.

        NOTE(cade): This breaks abstraction boundaries pretty badly. The better
        design is to have the "move to CPU and serialize" sampling decision be
        done outside of the model/sampler; this way the "last-mile" worker
        object which interfaces with the scheduler can serialize and incur the
        performance hit as necessary. This allows us to run the worker several
        iterations in a row without incurring the "move to CPU and serialize"
        performance penalty.

        Since this requires a large change to vLLM, we defer it to later and
        temporarily accept this broken abstraction boundary.

        NOTE(cade): This will require a special check if the proposer worker
        does not have a sampler (e.g. ngram speculation).
        """
        (self.scorer_worker.model_runner.sampler.include_gpu_probs_tensor
         ) = True
        (self.scorer_worker.model_runner.sampler.
         should_modify_greedy_probs_inplace) = True
        self.proposer_worker.set_include_gpu_probs_tensor()
        self.proposer_worker.set_should_modify_greedy_probs_inplace()

    def determine_num_available_blocks(self) -> Tuple[int, int]:
        """Determine the number of cache blocks to use.

        This is done by profiling the scorer model (which is typically the
        larger of the two). Then the total memory which would be used by the
        scorer cache is divided evenly between the proposer and scorer model KV,
        such that the number of blocks is equal in both KV caches.
        """
        num_gpu_blocks, num_cpu_blocks = (
            self.scorer_worker.determine_num_available_blocks())

        scorer_cache_block_size_bytes = (
            self.scorer_worker.get_cache_block_size_bytes())
        proposer_cache_block_size_bytes = (
            self.proposer_worker.get_cache_block_size_bytes())

        new_num_gpu_blocks = split_num_cache_blocks_evenly(
            scorer_cache_block_size_bytes, proposer_cache_block_size_bytes,
            num_gpu_blocks)
        return new_num_gpu_blocks, num_cpu_blocks

    def initialize_cache(self, num_gpu_blocks: int,
                         num_cpu_blocks: int) -> None:
        """Initialize the cache engine of the scorer and proposer workers.
        """
        self.scorer_worker.initialize_cache(num_gpu_blocks=num_gpu_blocks,
                                            num_cpu_blocks=num_cpu_blocks)
        self.proposer_worker.initialize_cache(num_gpu_blocks=num_gpu_blocks,
                                              num_cpu_blocks=num_cpu_blocks)

    def get_model(self) -> nn.Module:
        return self.scorer_worker.get_model()

    @torch.inference_mode()
    def execute_model(
        self,
        execute_model_req: Optional[ExecuteModelRequest] = None
    ) -> List[SamplerOutput]:
        """Perform speculative decoding on the input batch.
        """
        if self.rank != self._driver_rank:
            self._run_non_driver_rank()
            return []

        if execute_model_req is None:
            # This signals that there's no more requests to process for now.
            # All workers are running infinite loop with broadcast_tensor_dict,
            # and it stops the loop when the driver broadcasts an empty input.
            # Send an empty input to notify all other workers to stop their
            # execution loop.
            broadcast_tensor_dict({}, src=0)
            return []

        self._track_finished_requests(execute_model_req)
        disable_all_speculation = self._should_disable_all_speculation(
            execute_model_req)
        num_lookahead_slots = execute_model_req.num_lookahead_slots
        all_prompt = True
        atleast_one_prompt = False
        all_zero_spec_tokens = True
        for sgm in execute_model_req.seq_group_metadata_list:
            all_prompt = all_prompt and sgm.is_prompt
            atleast_one_prompt = atleast_one_prompt or sgm.is_prompt
            all_zero_spec_tokens = all_zero_spec_tokens and (
                sgm.num_speculative_tokens == 0)

        if all_prompt and execute_model_req.seq_group_metadata_list:
            assert num_lookahead_slots == 0, (
                "Prompt only runs should have num_lookahead_slots equal to 0. "
                "This should never happen, please file a bug at "
                "https://github.com/vllm-project/vllm/issues")
        # Speculative decoding is disabled in the following cases:
        # 1. Prefill phase: Speculative decoding is not
        #    used during the prefill phase.
        # 2. Auto-disable enabled: The running queue size exceeds
        #    the specified threshold.
        # 3. No request: There are no requests in the batch, or
        #    none of the requests in the batch have spec decoding enabled.
        # In any of these cases, the proposer and scorer workers
        # are called normally.
        # We expect `num_speculative_tokens` to be None for prefills.
        no_spec = (num_lookahead_slots == 0 or disable_all_speculation
                   or all_zero_spec_tokens)

        # Broadcast how many lookahead slots are scheduled for this step, and
        # whether all speculation is disabled, to all non-driver workers.

        # This is required as if the number of draft model runs changes
        # dynamically, the non-driver workers won't know unless we perform a
        # communication to inform them.

        # no_spec is used to signal non-driver worker about prefill vs decode
        # stage. This is needed to ensure that order of execution of proposer
        # and scorer is same in both driver and non-driver workers (i.e.,
        # scorer -> proposer for prefill and proposer -> scorer in decode). This
        # order is needed to support models like EAGLE that take scorer states
        # as inputs.
        broadcast_dict = dict(
            num_lookahead_slots=num_lookahead_slots,
            no_spec=no_spec,
            disable_all_speculation=disable_all_speculation,
            # When both chunked prefill and speculative decoding are enabled
            # it is possible that the same batch contains both prefill
            # and decodes. If that happens in the scorer we run the batch
            # as one single forward pass. However, in the proposer we
            # run them as 2 different batches - one for prefill and
            # the other for decodes. The variable indicates to the non-driver
            # worker that there are prefills as part of the speculative batch
            # and hence it needs to run an extra prefill forward pass.
            run_spec_proposer_for_prefill=atleast_one_prompt,
        )
        broadcast_tensor_dict(broadcast_dict, src=self._driver_rank)

        assert execute_model_req.seq_group_metadata_list is not None, (
            "speculative decoding requires non-None seq_group_metadata_list")

        self._maybe_disable_speculative_tokens(
            disable_all_speculation, execute_model_req.seq_group_metadata_list)

        if no_spec:
            return self._run_no_spec(execute_model_req,
                                     skip_proposer=disable_all_speculation)
        return self._run_speculative_decoding_step(execute_model_req,
                                                   num_lookahead_slots)

    @torch.inference_mode()
    def start_worker_execution_loop(self) -> None:
        """Execute model loop to perform speculative decoding
        in parallel worker."""
        while self._run_non_driver_rank():
            pass

    def _should_disable_all_speculation(
            self, execute_model_req: ExecuteModelRequest) -> bool:
        # When the batch size is too large, disable speculative decoding
        # to stop trading off throughput for latency.
        return (execute_model_req.running_queue_size
                >= self.disable_by_batch_size)

    def _maybe_disable_speculative_tokens(
            self, disable_all_speculation: bool,
            seq_group_metadata_list: List[SequenceGroupMetadata]) -> None:
        if not disable_all_speculation:
            return

        for seq_group_metadata in seq_group_metadata_list:
            # Once num_speculative_tokens is set to 0, the spec decode
            # of this request will be disabled forever.
            # TODO(comaniac): We currently store spec decoding specific
            # state in the global data structure, but we should maintain
            # this state within spec decode worker.
            seq_group_metadata.num_speculative_tokens = 0

    def _serialize_sampler_output_no_logprobs(
            self, execute_model_req: ExecuteModelRequest,
            sampler_output: SamplerOutput) -> List[SamplerOutput]:
        """
        Creates and returns a `SamplerOutput` with only the token IDs being
        serialized to CPU and populated in `CompletionSequenceGroupOutput`.
        All other parameters in `CompletionSequenceGroupOutput` related to log 
        probabilities are skipped.

        Args:
            execute_model_req (ExecuteModelRequest): The model request that
            was executed.
            sampler_output (SamplerOutput): The output from the sampler with
            only GPU tensors populated.

        Returns:
            SamplerOutput: A new `SamplerOutput` instance containing a list of 
            `CompletionSequenceGroupOutput` objects with only token IDs
            populated.
        """
        seq_output_prompt_logprobs = [
            seq.is_prompt and seq.sampling_params.prompt_logprobs is not None
            and seq.sampling_params.prompt_logprobs > 0
            for seq in execute_model_req.seq_group_metadata_list
        ]
        # ignore slots for prompt tokens that are filled with INVALID_TOKEN_ID
        sampled_token_ids_list = (sampler_output.sampled_token_ids[torch.where(
            # subtracting is faster than testing for equality
            sampler_output.sampled_token_ids - VLLM_INVALID_TOKEN_ID)[0]] \
            if any(seq_output_prompt_logprobs) else \
                sampler_output.sampled_token_ids).tolist()

        seq_data_entries = [
            (seq_id, seq_data) for sg in \
            execute_model_req.seq_group_metadata_list \
            for seq_id, seq_data in sg.seq_data.items()
        ]
        completion_seq_group_output_list: List[
            CompletionSequenceGroupOutput] = []
        output_index = 0
        # Make sure the non-terminal prefill chunks are still aligned with
        # their own empty output.
        for idx, seq_group_meta in enumerate(
                execute_model_req.seq_group_metadata_list):
            needs_prompt_logprobs = seq_output_prompt_logprobs[idx]
            seq_id, seq_data = seq_data_entries[idx]
            if needs_prompt_logprobs:
                prompt_token_ids = seq_data.get_prompt_token_ids()

                # Some of these sequences may belong to non-terminal chunks,
                # which may still have to report logprobs for prompts.
                start = 1 if seq_data._num_computed_tokens == 0 \
                    else seq_data._num_computed_tokens
                end = (seq_data._num_computed_tokens + \
                       seq_group_meta.token_chunk_size)
                prompt_token_ids = prompt_token_ids[start:end]
                prompt_logprobs = [
                    create_logprobs_output(
                        token_id=p_token_id,
                        token_id_logprob_rank=-1,
                        token_id_logprob=0.0,
                        topk_token_ids=[],
                        topk_logprobs=[],
                    ) for p_token_id in prompt_token_ids
                ]
            else:
                prompt_logprobs = None

            # Since we can get chunks here, we dont always have a sampled token
            # (only on last chunk) but we still have to provide an output.
            if not seq_group_meta.do_sample:
                completion_seq_group_output_list.append(
                    CompletionSequenceGroupOutput(
                        samples=[], prompt_logprobs=prompt_logprobs))
                continue

            # Sequence with output.
            completion_seq_group_output_list.append(
                create_sequence_group_output(
                    token_id=sampled_token_ids_list[output_index][0],
                    token_id_logprob_rank=-1,
                    token_id_logprob=0.0,
                    seq_id=seq_id,
                    topk_token_ids=[],
                    topk_logprobs=[],
                    prompt_logprobs=prompt_logprobs))
            output_index += 1

        return [SamplerOutput(outputs=completion_seq_group_output_list)]

    @nvtx_range("spec_decode_worker._run_no_spec")
    def _run_no_spec(self, execute_model_req: ExecuteModelRequest,
                     skip_proposer: bool) -> List[SamplerOutput]:
        """Run a single generation step without any speculation. The input is
        sent to the proposer and scorer model so that the KV cache is consistent
        between the two. When skip_proposer is True, the proposer model is
        not called, meaning that the kv-cache in proposer for requests is not
        updated, so they cannot enable spec decode in the rest decoding.
        """

        sampler_output = self.scorer_worker.execute_model(execute_model_req)
        assert len(sampler_output) == 1
        sampler_output = sampler_output[0]

        # Store hidden states from target model execution, BxD.
        hidden_states = sampler_output.hidden_states
        if hidden_states is not None:
            # Only decodes and prefill terminal chunks need a hidden state.
            seq_group_meta_with_hidden = [
                sg for sg in execute_model_req.seq_group_metadata_list
                if sg.do_sample
            ]
            if any(seq.is_prompt for seq in seq_group_meta_with_hidden):
                # Drop hidden_states with no prediction (eg non-terminal chunks)
                hidden_states = hidden_states[
                    torch.where(sampler_output.sampled_token_ids -
                                VLLM_INVALID_TOKEN_ID)[0]]
            if self.previous_hidden_states is None and len(
                    seq_group_meta_with_hidden):
                self.previous_hidden_states = HiddenStates(
                    hidden_states, seq_group_meta_with_hidden)
            elif self.previous_hidden_states and len(
                    seq_group_meta_with_hidden):
                self.previous_hidden_states.update(hidden_states,
                                                   seq_group_meta_with_hidden)
                self.previous_hidden_states.prune(seq_group_meta_with_hidden)

        if not skip_proposer:
            # We prepare the prefill hidden states here so that there no
            # additional complexity in worker for spec_decode vs non_spec_decode
            # flow and execute_model doesn't need additional modifications.
            execute_model_req.previous_hidden_states = \
                prepare_prefill_hidden_states(
                    sampler_output.prefill_hidden_states)
            for i in range(self._num_spec_prefill_steps):
                execute_model_req.spec_step_idx = i
                self.proposer_worker.execute_model(execute_model_req)

        sampler_output_to_return = (self._serialize_sampler_output_no_logprobs(
            execute_model_req=execute_model_req, sampler_output=sampler_output)
                                    if self._disable_logprobs else
                                    [sampler_output])

        # Clear device tensors from sampler output. This reduces communication
        # overhead when the engine runs in a different process than the workers.
        sampler_output.sampled_token_probs = None
        sampler_output.sampled_token_ids = None
        sampler_output.logprobs = None
        return sampler_output_to_return

    def _run_non_driver_rank(self) -> bool:
        """Run proposer and verifier model in non-driver workers. This is used
        for both speculation cases (num_lookahead_slots>0) and non-speculation
        cases (e.g. prefill).

        Returns True if there are remaining sequences to process.
        """
        assert self.rank != self._driver_rank

        data = broadcast_tensor_dict(src=self._driver_rank)
        if not data:
            return False
        num_lookahead_slots = data["num_lookahead_slots"]

        # In case of prefill, scorer_worker has to be run before proposer so
        # that the hidden states can be propagated to proposer when needed.
        if data["no_spec"]:
            self.scorer_worker.execute_model()

        if not data["disable_all_speculation"]:
            # Even if num_lookahead_slots is zero, we want to run the
            # proposer model as it may have KV.
            #
            # We run the proposer once per lookahead slot. In the future we
            # should delegate how many times it runs to the proposer.
            for _ in range(max(num_lookahead_slots, 1)):
                self.proposer_worker.execute_model()

        if not data["no_spec"]:
            self.scorer_worker.execute_model()
            if data["run_spec_proposer_for_prefill"]:
                self.proposer_worker.execute_model()

        return True

    @nvtx_range("spec_decode_worker._run_speculative_decoding_step")
    def _run_speculative_decoding_step(
            self, execute_model_req: ExecuteModelRequest,
            num_lookahead_slots: int) -> List[SamplerOutput]:
        """Execute a single step of speculative decoding.

        This invokes the proposer worker to get k speculative tokens for each
        sequence, then scores each speculative token using the scoring worker.

        When `enable_chunked_prefill` is set, scorer will batch decodes and 
        prefills, while proposer will sync its KV-cache by running an extra
        forward on prefills.

        Returns a list of SamplerOutput, each containing a single token per
        sequence.
        """
        # With prefill chunking, expect requests to have prompts first
        # so that backend gets prefill|decode.
        assert num_lookahead_slots == execute_model_req.num_lookahead_slots

        # Pass last hidden states from target model to proposer
        execute_model_req.previous_hidden_states = self.previous_hidden_states
        self.previous_hidden_states = None

        with Timer() as proposal_timer:
            # Generate proposals using draft worker.
            proposals = self.proposer_worker.get_spec_proposals(
                execute_model_req, self._seq_with_bonus_token_in_last_step)
        # logger.info("Here is the proposed")
        # logger.info(proposals)
        # 1) Top‑10 from the draft distributions
        batch_size, k, vocab_size = proposals.proposal_probs.shape
        for b in range(batch_size):
            logger.info(f"\n--- Batch item {b} draft distributions ---")
            for i in range(k):
                dist = proposals.proposal_probs[b, i]            # shape: (vocab_size,)
                top_probs, top_ids = torch.topk(dist, k=10)      # top-10 log‑probs and ids
                # toks = tokenizer.convert_ids_to_tokens(top_ids.tolist())
                logger.info(f"Lookahead slot {i}:")
                for rank, (tid, prob) in enumerate(zip(top_ids.tolist(), top_probs.tolist()), start=1):
                    logger.info(f"  {rank:>2}. (id {tid:>6}) → log‑prob {prob:.4f}")

        if not self._allow_zero_draft_token_step and proposals.no_proposals:
            #TODO: Fix it #5814
            raise RuntimeError("Cannot handle cases where distributed draft "
                               "workers generate no tokens")

        execute_model_req.previous_hidden_states = None

        with Timer() as scoring_timer:
            proposal_scores = self.scorer.score_proposals(
                execute_model_req,
                proposals,
            )

        logger.info("Here is the scored")
        logger.info(proposal_scores)

        logger.info("Here is the scored")
        logger.info("probs tensor: %s", proposal_scores.probs)
        logger.info("token_ids tensor: %s", proposal_scores.token_ids)

        # logger.info("\n--- Accepted tokens per sequence ---")
        # for b, tid in enumerate(accepted_token_ids):
        #     tok = tokenizer.decode([int(tid)])
        #     logp = float(target_logprobs[b]) if target_logprobs is not None else None
        #     logger.info(f"Sequence {b}: accepted {tok!r} (id {tid})"
        #           + (f" with target log‑prob {logp:.4f}" if logp is not None else ""))

        _, (non_spec_seqs, non_spec_indices) = split_batch_by_proposal_len(
            execute_model_req.seq_group_metadata_list, proposals.proposal_lens)
        # With prefill chunking enabled, `non_spec_seqs` contains prefills too:
        # discard decodes that have already been processed by proposer.
        non_spec_indices = [
            idx for idx in non_spec_indices
            if execute_model_req.seq_group_metadata_list[idx].is_prompt
        ]
        if len(non_spec_indices):
            all_hidden_states = proposal_scores.hidden_states
            if all_hidden_states is not None:
                prefill_hidden_states = all_hidden_states[non_spec_indices]
                execute_model_req.previous_hidden_states = \
                    prepare_prefill_hidden_states(prefill_hidden_states)
            # Sync proposer KV cache for prefills.
            prefill_req = execute_model_req.clone(non_spec_seqs)
            # TODO avoid sampling here?
            self.proposer_worker.execute_model(prefill_req)

        with Timer() as verification_timer:
            accepted_token_ids, target_logprobs = self._verify_tokens(
                execute_model_req.seq_group_metadata_list, proposal_scores,
                proposals, execute_model_req.num_lookahead_slots)

        stage_times = (proposal_timer.elapsed_time_ms / num_lookahead_slots,
                       scoring_timer.elapsed_time_ms,
                       verification_timer.elapsed_time_ms)

        return self._create_output_sampler_list(
            execute_model_req.seq_group_metadata_list,
            accepted_token_ids,
            target_logprobs=target_logprobs,
            prompt_logprobs=proposal_scores.prompt_logprobs
            if not self._disable_logprobs else None,
            k=execute_model_req.num_lookahead_slots,
            stage_times=stage_times)

    @nvtx_range("spec_decode_worker._verify_tokens")
    def _verify_tokens(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        proposal_scores: SpeculativeScores,
        proposals: SpeculativeProposals,
        max_proposal_len: int,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Determine which speculative tokens are accepted using the
        probabilities of each token according to the proposer and scorer models.

        Returns a tuple of Tensors, one for the accepted token ids and one for
        the logprobs according to the scoring model.
        """
        proposal_lens_list = proposals.proposal_lens.tolist()

        # vLLM currently only supports proposal lens equal to zero or the batch
        # proposal len. This adds some complexity (splitting the batch into spec
        # and non spec sequences) and should be removed in the future. It can be
        # done by supporting per-sequence proposal lens.
        (_, spec_indices), (_, non_spec_indices) = split_batch_by_proposal_len(
            seq_group_metadata_list, proposal_lens_list)
        original_indices = spec_indices + non_spec_indices

        # Get probabilities of target model, including bonus tokens.
        proposal_verifier_probs = proposal_scores.probs[spec_indices]

        # Get non-speculative sampled tokens from target model.
        non_spec_token_ids = proposal_scores.token_ids[non_spec_indices]

        # Get bonus tokens from target model.
        bonus_token_ids = proposal_scores.token_ids[spec_indices, -1:]

        # Get probabilities according to proposal method.
        proposal_probs = proposals.proposal_probs[spec_indices]

        # Get proposed tokens.
        proposal_token_ids = proposals.proposal_token_ids[spec_indices]

        # Sampler arguments
        sampler_extra_kwargs: Dict[str, Any] = {}
        if self.generators and isinstance(self.spec_decode_sampler,
                                          SpecDecodeStochasticBaseSampler):
            sampler_extra_kwargs["seeded_seqs"] = {
                idx: self.generators[sgm.request_id]
                for idx, sgm in enumerate(seq_group_metadata_list)
                if sgm.sampling_params.seed is not None
            }

        accepted_token_ids = self.spec_decode_sampler(
            target_with_bonus_probs=proposal_verifier_probs,
            bonus_token_ids=bonus_token_ids,
            draft_probs=proposal_probs,
            draft_token_ids=proposal_token_ids,
            **sampler_extra_kwargs,
        )
        # Append output tokens from non-speculative sequences to
        # the accepted token ids tensor.
        non_spec_token_ids = non_spec_token_ids.expand(-1, max_proposal_len +
                                                       1).clone()
        non_spec_token_ids[:, 1:] = -1
        accepted_token_ids = torch.cat(
            [accepted_token_ids, non_spec_token_ids])
        logprobs = proposal_scores.logprobs
        # Rearrange so that results are in the order of the original seq group
        # metadata.
        accepted_token_ids[original_indices] = accepted_token_ids.clone()

        # B x K+1 x D
        hidden_states = proposal_scores.hidden_states
        if hidden_states is not None:
            # Only get terminal hidden states for next step
            terminal_metadata = [
                sg for sg in seq_group_metadata_list if sg.do_sample
            ]

            # Contract hidden states based on accepted tokens
            hs_size = hidden_states.shape[-1]
            accepted_index = accepted_token_ids + 1  # Convert -1 to 0
            accepted_index = accepted_index.count_nonzero(dim=1).add_(-1)  # b
            # Drop non-terminal prefill chunks hidden states.
            hidden_states = hidden_states[accepted_index !=
                                          VLLM_INVALID_TOKEN_ID]
            accepted_index = accepted_index[accepted_index !=
                                            VLLM_INVALID_TOKEN_ID]
            assert len(accepted_index) == hidden_states.shape[0] == len(
                terminal_metadata)
            index = accepted_index[:, None, None].expand(-1, 1,
                                                         hs_size)  # b x 1 x d
            second_last_token_hidden_states = hidden_states[:, -2]  # b x d
            hidden_states = hidden_states.gather(1, index).squeeze(1)  # b x d
            # Store hidden states from target model for subsequent decode step
            self.previous_hidden_states = HiddenStates(
                hidden_states, terminal_metadata,
                second_last_token_hidden_states)
        return accepted_token_ids, logprobs

    def _create_output_sampler_list(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        accepted_token_ids: torch.Tensor,  # shape: [batch_size, k+1]
        target_logprobs: torch.Tensor,  # shape: [batch_size, k+1, vocab_size]
        prompt_logprobs: Optional[
            torch.Tensor],  # shape: [nprompt_tokens, vocab_size]
        k: int,
        stage_times: Tuple[float, float, float],
    ) -> List[SamplerOutput]:
        """Given the accepted token ids, create a list of SamplerOutput.

        The output is padded with -1 tokens such that each sequence has
        the same number of outputs.
        """
        batch_size, num_steps = accepted_token_ids.shape
        accepted_token_ids_by_step = accepted_token_ids.transpose(0, 1)
        if self._disable_logprobs:
            # We are skipping the logprobs. Hence don't serialize the
            # logprobs related tensors from the GPU. Instead create
            # empty/dummy lists.
            (accepted_token_id_ranks_by_step,
            accepted_token_id_logprobs_by_step,
            topk_logprobs_by_step, topk_indices_by_step) =\
            self._create_dummy_logprob_lists(
                batch_size, num_steps,
                self.scorer_worker.model_config.max_logprobs)
        else:
            # Organize input tensors by step instead of by sequence.
            target_logprobs_by_step = target_logprobs.transpose(0, 1)
            # Serialize all tensors into Python lists.
            (accepted_token_id_ranks_by_step,
            accepted_token_id_logprobs_by_step,
            topk_logprobs_by_step, topk_indices_by_step) =\
                self._create_logprob_lists_from_tensors(
                    target_logprobs_by_step, accepted_token_ids_by_step,
                    self.scorer_worker.model_config.max_logprobs)

        # Get the sequence ids and num_logprobs (sampling parameter) in the
        # batch.
        seq_ids, request_ids_seq_ids_mapping = get_all_seq_ids_and_request_ids(
            seq_group_metadata_list)

        num_logprobs_per_seq = get_all_num_logprobs(seq_group_metadata_list)

        # Serialize tensor to CPU Python list.
        accepted_token_ids_by_step = accepted_token_ids_by_step.tolist()

        # Construct the output on a per-step, per-sequence basis.
        # Non-terminal prefill chunks will end up here as rows with just -1s
        # i.e mixed-batch [[-1, 1576], [-1, 29884], [-1, -1], [-1, -1]] while
        # terminal chunks will only have one generated token at time 0.
        sampler_output_list: List[SamplerOutput] = []

        # Prefills are not multi-step (return at most 1 token), in order to
        # avoid padding or repetition to fit decodes, we separate them.
        for i, sg in enumerate(seq_group_metadata_list):
            if not sg.is_prompt:
                # Requests are ordered as prefills|decodes=>no more prefills.
                break
            num_logprobs = num_logprobs_per_seq[i]
            seq_kwargs = dict(token_id=-1,
                              token_id_logprob_rank=0,
                              token_id_logprob=-float('inf'),
                              topk_token_ids=[-1] * num_logprobs,
                              topk_logprobs=[-float('inf')] * num_logprobs,
                              seq_id=seq_ids[i])
            # Terminal chunk, has token.
            if sg.do_sample:
                seq_kwargs.update(
                    dict(
                        token_id=accepted_token_ids[i][0].item(),
                        token_id_logprob_rank=accepted_token_id_ranks_by_step[
                            0][i],
                        token_id_logprob=accepted_token_id_logprobs_by_step[0]
                        [i],
                        topk_token_ids=topk_indices_by_step[0][i]
                        [:num_logprobs],
                        # output only so step is 0
                        topk_logprobs=topk_logprobs_by_step[0][i]
                        [:num_logprobs],
                    ))
            needs_plogs = (sg.sampling_params.prompt_logprobs
                           and sg.sampling_params.prompt_logprobs > 0)
            plogs = None
            if prompt_logprobs is not None:
                # Even non-terminal prompt chunks can have logprobs here.
                plogs = prompt_logprobs[i]
            elif needs_plogs:
                # Prompt logprobs are requested but `_disable_logprobs` is set.
                seq_data = next(iter(sg.seq_data.values()))
                # Get only the tokens in this chunk!
                prompt_token_ids = seq_data.get_prompt_token_ids()
                prompt_token_ids = prompt_token_ids[
                    seq_data.
                    _num_computed_tokens:seq_data._num_computed_tokens +
                    sg.token_chunk_size]

                is_first_chunk = seq_data._num_computed_tokens == 0
                # There's no prob generated for the first token in a sequence.
                if is_first_chunk:
                    prompt_token_ids = prompt_token_ids[1:]
                plogs = [
                    create_logprobs_output(
                        token_id=p_token_id,
                        token_id_logprob_rank=-1,
                        token_id_logprob=0.0,
                        topk_token_ids=[],
                        topk_logprobs=[],
                    ) for p_token_id in prompt_token_ids
                ]
            seq_kwargs.update(dict(prompt_logprobs=plogs))

            sampler_output_list.append(
                SamplerOutput(
                    outputs=[create_sequence_group_output(
                        **seq_kwargs)]))  # type: ignore

        # Decodes, create one SamplerOutput per-step (at most K+1).
        for step_index in range(num_steps):
            if all(token_id == -1 for sg, token_id in zip(
                    seq_group_metadata_list,
                    accepted_token_ids_by_step[step_index])
                   if not sg.is_prompt):
                break

            step_output_token_ids: List[CompletionSequenceGroupOutput] = []
            for sequence_index in range(batch_size):
                seq_meta = seq_group_metadata_list[sequence_index]
                # Prompts already processed above.
                if seq_meta.is_prompt:
                    continue

                # Each sequence may have a different num_logprobs; retrieve it.
                num_logprobs = num_logprobs_per_seq[sequence_index]
                step_output_token_ids.append(
                    create_sequence_group_output(
                        token_id=accepted_token_ids_by_step[step_index]
                        [sequence_index],
                        token_id_logprob_rank=accepted_token_id_ranks_by_step[
                            step_index][sequence_index],
                        token_id_logprob=accepted_token_id_logprobs_by_step[
                            step_index][sequence_index],
                        seq_id=seq_ids[sequence_index],
                        topk_token_ids=topk_indices_by_step[step_index]
                        [sequence_index][:num_logprobs],
                        topk_logprobs=topk_logprobs_by_step[step_index]
                        [sequence_index][:num_logprobs],
                        step_index=step_index))
            sampler_output_list.append(
                SamplerOutput(outputs=step_output_token_ids))

        # Populate the data structures needed to keep track of sequences with
        # bonus tokens.
        self._track_sequences_with_bonus_tokens(seq_ids,
                                                request_ids_seq_ids_mapping,
                                                accepted_token_ids_by_step)
        maybe_rejsample_metrics = (
            self._metrics.maybe_collect_rejsample_metrics(k))
        if maybe_rejsample_metrics is not None:
            sampler_output_list[
                0].spec_decode_worker_metrics = maybe_rejsample_metrics

            # Log time spent in each stage periodically.
            # This is periodic because the rejection sampler emits metrics
            # periodically.
            self._maybe_log_stage_times(*stage_times)
        # First `n_prefills` entries will contain prefills SamplerOutput when
        # chunked prefill is enabled, the rest is decodes in multi-step format.
        return sampler_output_list

    def _maybe_log_stage_times(self, average_time_per_proposal_tok_ms: float,
                               scoring_time_ms: float,
                               verification_time_ms: float) -> None:
        """Log the speculative stage times. If stat logging is disabled, do
        nothing.
        """
        if self._disable_log_stats:
            return

        logger.info(
            "SpecDecodeWorker stage times: "
            "average_time_per_proposal_tok_ms=%.02f "
            "scoring_time_ms=%.02f verification_time_ms=%.02f",
            average_time_per_proposal_tok_ms, scoring_time_ms,
            verification_time_ms)

    def _create_dummy_logprob_lists(
        self,
        batch_size: int,
        num_steps: int,
        num_top_k: int,
    ) -> Tuple[List[List[int]], List[List[float]],
               List[List[List[Optional[float]]]],
               List[List[List[Optional[int]]]]]:
        """
        Creates and returns four dummy lists representing token probabilities 
        and their ranks.

        This method initializes and returns:
            - The ranks of the accepted tokens, shaped (num_steps, batch_size)
            - The log probabilities of the accepted tokens,
              shaped (num_steps, batch_size)
            - The log probabilities of the top k tokens,
              shaped (num_steps, batch_size, num_top_k)
            - The token IDs of the top k tokens,
              shaped (num_steps, batch_size, num_top_k)

        Args:
            batch_size (int): The size of the batch.
            num_steps (int): The number of steps in the sequence.
            num_top_k (int): The number of top-k token log probabilities to
            return.
        
        Returns:
            A tuple containing four dummy lists as described above.
        """
        accepted_token_id_ranks_by_step = [[-1] * batch_size
                                           for _ in range(num_steps)]
        accepted_token_id_logprobs_by_step = [[0.0] * batch_size
                                              for _ in range(num_steps)]
        topk_logprobs_by_step: List[List[List[Optional[float]]]] = [[
            [None] * num_top_k for _ in range(batch_size)
        ] for _ in range(num_steps)]
        topk_indices_by_step: List[List[List[Optional[int]]]] = [[
            [None] * num_top_k for _ in range(batch_size)
        ] for _ in range(num_steps)]
        return (accepted_token_id_ranks_by_step,
                accepted_token_id_logprobs_by_step, topk_logprobs_by_step,
                topk_indices_by_step)

    def _create_logprob_lists_from_tensors(
        self,
        target_logprobs_by_step: torch.Tensor,
        accepted_token_ids_by_step: torch.Tensor,
        num_top_k: int,
    ) -> Tuple[List[List[int]], List[List[float]],
               List[List[List[Optional[float]]]],
               List[List[List[Optional[int]]]]]:
        """
        Creates and returns four lists representing token probabilities and
        their ranks.

        This method initializes and returns four lists containing:
            - The ranks of the accepted tokens, shaped (num_steps, batch_size)
            - The log probabilities of the accepted tokens,
              shaped (num_steps, batch_size)
            - The log probabilities of the top k tokens,
              shaped (num_steps, batch_size, num_top_k)
            - The token IDs of the top k tokens,
              shaped (num_steps, batch_size, num_top_k)

        Args:
            target_logprobs_by_step (torch.Tensor): Tensor representing the
            log probabilities of the target model,
            shaped (num_steps, batch_size, vocab_size)
            accepted_token_ids_by_step (torch.Tensor): Tensor representing
            the accepted  token_ids, shaped (num_steps, batch_size) 
            num_top_k (int): The number of top-k token log probabilities to
            return.
        
        Returns:
            A tuple containing the lists as described above.
        """
        # Serialize all tensors to CPU Python lists.
        # Get the logprobs/rank of the accepted tokens.
        (accepted_token_id_ranks_by_step_tensor,
         accepted_token_id_logprobs_by_step_tensor
         ) = get_sampled_token_logprobs(
             logprob_tensor=target_logprobs_by_step,
             sampled_token_ids=accepted_token_ids_by_step,
         )
        # Get the top-k logprobs (which may or may not include the
        # logprob of the accepted token).
        (topk_logprobs_by_step_tensor,
         topk_indices_by_step_tensor) = target_logprobs_by_step.topk(
             k=num_top_k,
             dim=-1,
         )
        accepted_token_id_ranks_by_step = (
            accepted_token_id_ranks_by_step_tensor.tolist())
        accepted_token_id_logprobs_by_step = (
            accepted_token_id_logprobs_by_step_tensor.tolist())
        topk_logprobs_by_step = topk_logprobs_by_step_tensor.tolist()
        topk_indices_by_step = topk_indices_by_step_tensor.tolist()
        return (accepted_token_id_ranks_by_step,
                accepted_token_id_logprobs_by_step, topk_logprobs_by_step,
                topk_indices_by_step)

    def _track_finished_requests(self, execute_model_req: ExecuteModelRequest):
        """
        Removes the finished requests and their associated sequence ids from
        internal book keeping data structures.
        """
        for finished_request in execute_model_req.finished_requests_ids:
            for seq_id in self._request_id_seq_id_mapping[finished_request]:
                self._seq_with_bonus_token_in_last_step.discard(seq_id)
            del self._request_id_seq_id_mapping[finished_request]

    def _track_sequences_with_bonus_tokens(
            self, seq_ids: List[int],
            request_ids_seq_ids_mapping: Dict[str, Set[int]],
            accepted_token_ids_by_step: List[List[int]]):
        """
        Updates the internal data structures which keep track of sequences
        which have been assigned bonus tokens in their last forward pass.
        """
        for seq_index, seq_id in enumerate(seq_ids):
            last_token_id = accepted_token_ids_by_step[-1][seq_index]
            if last_token_id == -1:
                self._seq_with_bonus_token_in_last_step.discard(seq_id)
            else:
                self._seq_with_bonus_token_in_last_step.add(seq_id)
        for request_id, sequences in request_ids_seq_ids_mapping.items():
            self._request_id_seq_id_mapping[request_id].update(sequences)

    @cached_property
    def _vocab_size(self) -> int:
        """Get the vocab size of the model and make sure it's consistent between
        draft and target workers.
        """
        vocab_sizes = [
            worker.vocab_size
            for worker in [self.proposer_worker, self.scorer_worker]
        ]
        assert all(vocab_sizes[0] == vocab_size for vocab_size in vocab_sizes)
        return vocab_sizes[0]

    @property
    def rank(self):
        return self.scorer_worker.rank

    @property
    def device(self):
        return self.scorer_worker.device

    @property
    def _driver_rank(self) -> int:
        return 0

    def get_cache_block_size_bytes(self):
        """Return the size of a cache block in bytes.
        
        This function is only used to compose workers within a SpecDecodeWorker.
        We leave composing a SpecDecodeWorker within a SpecDecodeWorker
        undefined for now, although it could be implemented in the future.
        See https://arxiv.org/abs/2308.04623.
        """
        raise NotImplementedError

    def start_profile(self):
        if isinstance(self.scorer_worker, WorkerBase):
            self.scorer_worker.start_profile()

    def stop_profile(self):
        if isinstance(self.scorer_worker, WorkerBase):
            self.scorer_worker.stop_profile()


def split_num_cache_blocks_evenly(scorer_cache_block_size_bytes: int,
                                  proposer_cache_block_size_bytes: int,
                                  total_num_gpu_blocks: int) -> int:
    """Given total_num_gpu_blocks, the number of GPU blocks that could be
    allocate to the target model, this function calculates how many blocks
    should be given to the draft and target model.

    Note that usually the block size, in bytes, of each model is different,
    as it's a function of number of KV/layer, number of heads, and hidden
    dimension size.

    Since the target and draft models allocate the same number of blocks, we
    simply calculate the number of blocks where if allocated by both models,
    the total memory usage from KV cache is no larger than the number of
    blocks allocatable by the target model alone.
    """
    new_num_gpu_blocks = int(
        total_num_gpu_blocks * scorer_cache_block_size_bytes /
        (proposer_cache_block_size_bytes + scorer_cache_block_size_bytes))

    return new_num_gpu_blocks


def prepare_prefill_hidden_states(
        prefill_hidden_states: torch.Tensor) -> HiddenStates:
    # For prefill step in proposer, we run the model for N-1 tokens
    # because Nth token will be processed in the first decode step. For
    # N-1 tokens, the input should be 0:N-1 hidden states which should
    # be concatanated with 1:N token (since output of scorer has to be
    # the input for proposer). Therefore, we shift the hidden states to
    # align n-1th hidden state with nth token.
    return HiddenStates(prefill_hidden_states.roll(
        shifts=1, dims=0)) if prefill_hidden_states is not None else None
