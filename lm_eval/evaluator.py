import os
import itertools
import json
import jsonlines
import logging
import random
import time
import copy
from collections import defaultdict
from typing import TYPE_CHECKING, List, Optional, Union

import numpy as np
import torch

import lm_eval.api.metrics
import lm_eval.api.registry
import lm_eval.models
from lm_eval.caching.cache import delete_cache
from lm_eval.evaluator_utils import (
    consolidate_results,
    get_sample_size,
    get_task_list,
    prepare_print_tasks,
    print_writeout,
    run_task_tests,
)
from lm_eval.loggers import EvaluationTracker
from lm_eval.loggers.utils import add_env_info, get_git_commit_hash
from lm_eval.tasks import TaskManager, get_task_dict
from lm_eval.utils import (
    eval_logger,
    handle_non_serializable,
    hash_string,
    positional_deprecated,
    simple_parse_args_string,
)


if TYPE_CHECKING:
    from lm_eval.api.model import LM
    from lm_eval.tasks import Task


@positional_deprecated
def simple_evaluate(
    model,
    model_args: Optional[Union[str, dict]] = None,
    tasks: Optional[List[Union[str, dict, object]]] = None,
    num_fewshot: Optional[int] = None,
    batch_size: Optional[int] = None,
    max_batch_size: Optional[int] = None,
    device: Optional[str] = None,
    use_cache: Optional[str] = None,
    cache_requests: bool = False,
    rewrite_requests_cache: bool = False,
    delete_requests_cache: bool = False,
    limit: Optional[Union[int, float]] = None,
    bootstrap_iters: int = 100000,
    check_integrity: bool = False,
    write_out: bool = False,
    log_samples: bool = True,
    evaluation_tracker: Optional[EvaluationTracker] = None,
    system_instruction: Optional[str] = None,
    apply_chat_template: bool = False,
    fewshot_as_multiturn: bool = False,
    gen_kwargs: Optional[str] = None,
    task_manager: Optional[TaskManager] = None,
    verbosity: str = "INFO",
    predict_only: bool = False,
    random_seed: int = 0,
    numpy_random_seed: int = 1234,
    torch_random_seed: int = 1234,
    fewshot_random_seed: int = 1234,
    retrieval_args: dict = {},
):
    """Instantiate and evaluate a model on a list of tasks.

    :param model: Union[str, LM]
        Name of model or LM object, see lm_eval.models.get_model
    :param model_args: Optional[str, dict]
        String or dict arguments for each model class, see LM.create_from_arg_string and LM.create_from_arg_object.
        Ignored if `model` argument is a LM object.
    :param tasks: list[Union[str, dict, Task]]
        List of task names or Task objects. Task objects will be taken to have name task.EVAL_HARNESS_NAME if defined and type(task).__name__ otherwise.
    :param num_fewshot: int
        Number of examples in few-shot context
    :param batch_size: int or str, optional
        Batch size for model
    :param max_batch_size: int, optional
        Maximal batch size to try with automatic batch size detection
    :param device: str, optional
        PyTorch device (e.g. "cpu" or "cuda:0") for running models
    :param use_cache: str, optional
        A path to a sqlite db file for caching model responses. `None` if not caching.
    :param cache_requests: bool, optional
        Speed up evaluation by caching the building of dataset requests. `None` if not caching.
    :param rewrite_requests_cache: bool, optional
        Rewrites all of the request cache if set to `True`. `None` if not desired.
    :param delete_requests_cache: bool, optional
        Deletes all of the request cache if set to `True`. `None` if not desired.
    :param limit: int or float, optional
        Limit the number of examples per task (only use this for testing), If <1, limit is a percentage of the total number of examples.
    :param bootstrap_iters:
        Number of iterations for bootstrap statistics, used when calculating stderrs. set to 0 for no stderr calculations to be performed.
    :param check_integrity: bool
        Whether to run the relevant part of the test suite for the tasks
    :param write_out: bool
        If True, write out an example document and model input for checking task integrity
    :param log_samples: bool
        If True, write out all model outputs and documents for per-sample measurement and post-hoc analysis
    :param system_instruction: str
        System instruction to be applied to the prompt
    :param apply_chat_template: bool
        If True, apply chat template to the prompt
    :param fewshot_as_multiturn: bool
        Whether to provide the fewshot examples as a multiturn conversation or a single user turn.
    :param gen_kwargs: str
        String arguments for model generation
        Ignored for all tasks with loglikelihood output_type
    :param predict_only: bool
        If true only model outputs will be generated and returned. Metrics will not be evaluated
    :param random_seed: int
        Random seed for python's random module. If set to None, the seed will not be set.
    :param numpy_random_seed: int
        Random seed for numpy. If set to None, the seed will not be set.
    :param torch_random_seed: int
        Random seed for torch. If set to None, the seed will not be set.
    :param fewshot_random_seed: int
        Random seed for fewshot sampler random generator. If set to None, the seed of generator will be set to None.

    :return
        Dictionary of results
    """
    eval_logger.setLevel(getattr(logging, f"{verbosity}"))
    start_date = time.time()

    if delete_requests_cache:
        eval_logger.info("Deleting requests cache...")
        delete_cache()

    seed_message = []
    if random_seed is not None:
        # See https://github.com/EleutherAI/lm-evaluation-harness/pull/1412
        seed_message.append(f"Setting random seed to {random_seed}")
        random.seed(random_seed)

    if numpy_random_seed is not None:
        seed_message.append(f"Setting numpy seed to {numpy_random_seed}")
        np.random.seed(numpy_random_seed)

    if torch_random_seed is not None:
        seed_message.append(f"Setting torch manual seed to {torch_random_seed}")
        torch.manual_seed(torch_random_seed)

    if seed_message:
        eval_logger.info(" | ".join(seed_message))

    if tasks is None:
        tasks = []
    if len(tasks) == 0:
        raise ValueError(
            "No tasks specified, or no tasks found. Please verify the task names."
        )

    if gen_kwargs is not None:
        gen_kwargs = simple_parse_args_string(gen_kwargs)
        eval_logger.warning(
            "generation_kwargs specified through cli, these settings will update set parameters in yaml tasks. "
            "Ensure 'do_sample=True' for non-greedy decoding!"
        )
        if gen_kwargs == "":
            gen_kwargs = None

    if isinstance(model, str):
        if model_args is None:
            eval_logger.warning("model_args not specified. Using defaults.")
            model_args = ""

        if isinstance(model_args, dict):
            eval_logger.info(
                f"Initializing {model} model, with arguments: {model_args}"
            )
            lm = lm_eval.api.registry.get_model(model).create_from_arg_obj(
                model_args,
                {
                    "batch_size": batch_size,
                    "max_batch_size": max_batch_size,
                    "device": device,
                },
            )

        else:
            eval_logger.info(
                f"Initializing {model} model, with arguments: {simple_parse_args_string(model_args)}"
            )
            lm = lm_eval.api.registry.get_model(model).create_from_arg_string(
                model_args,
                {
                    "batch_size": batch_size,
                    "max_batch_size": max_batch_size,
                    "device": device,
                },
            )
    else:
        if not isinstance(model, lm_eval.api.model.LM):
            raise TypeError
        eval_logger.info("Using pre-initialized model")
        lm = model

    if use_cache is not None:
        eval_logger.info(f"Using cache at {use_cache + '_rank' + str(lm.rank) + '.db'}")
        lm = lm_eval.api.model.CachingLM(
            lm,
            use_cache
            # each rank receives a different cache db.
            # necessary to avoid multiple writes to cache at once
            + "_rank"
            + str(lm.rank)
            + ".db",
        )

    if task_manager is None:
        task_manager = TaskManager(verbosity)

    task_dict = get_task_dict(tasks, task_manager)
    for task_name in task_dict.keys():
        task_obj = task_dict[task_name]
        if isinstance(task_obj, tuple):
            _, task_obj = task_obj
            if task_obj is None:
                continue

        if task_obj.get_config("output_type") == "generate_until":
            if gen_kwargs is not None:
                task_obj.set_config(
                    key="generation_kwargs", value=gen_kwargs, update=True
                )

        if predict_only:
            log_samples = True
            eval_logger.info(
                f"Processing {task_name} in output-only mode. Metrics will not be calculated!"
            )
            # we have to change the class properties post-hoc. This is pretty hacky.
            task_obj.override_metric(metric_name="bypass")

        # override tasks' fewshot values to the provided num_fewshot arg value
        # except if tasks have it set to 0 manually in their configs--then we should never overwrite that
        if num_fewshot is not None:
            if (default_num_fewshot := task_obj.get_config("num_fewshot")) == 0:
                eval_logger.info(
                    f"num_fewshot has been set to 0 for {task_name} in its config. Manual configuration will be ignored."
                )
            else:
                eval_logger.warning(
                    f"Overwriting default num_fewshot of {task_name} from {default_num_fewshot} to {num_fewshot}"
                )
                task_obj.set_config(key="num_fewshot", value=num_fewshot)
        else:
            # if num_fewshot not provided, and the task does not define a default one, default to 0
            if (default_num_fewshot := task_obj.get_config("num_fewshot")) is None:
                task_obj.set_config(key="num_fewshot", value=0)
        # fewshot_random_seed set for tasks, even with a default num_fewshot (e.g. in the YAML file)
        task_obj.set_fewshot_seed(seed=fewshot_random_seed)
        eval_logger.info(
            f"Setting fewshot random generator seed to {fewshot_random_seed}"
        )

    if check_integrity:
        run_task_tests(task_list=tasks)

    if evaluation_tracker is not None:
        evaluation_tracker.general_config_tracker.log_experiment_args(
            model_source=model,
            model_args=model_args,
            system_instruction=system_instruction,
            chat_template=lm.chat_template if apply_chat_template else None,
        )

    if retrieval_args['brute_force_rag_eval']:
        num_rounds = 3
        for round in range(num_rounds):
            retrieval_args.update({'round': round})
            results = evaluate(
                lm=lm,
                task_dict=task_dict,
                limit=limit,
                cache_requests=cache_requests,
                rewrite_requests_cache=rewrite_requests_cache,
                bootstrap_iters=bootstrap_iters,
                write_out=write_out,
                log_samples=log_samples,
                system_instruction=system_instruction,
                apply_chat_template=apply_chat_template,
                fewshot_as_multiturn=fewshot_as_multiturn,
                verbosity=verbosity,
                retrieval_args=retrieval_args,
            )
    else:
        results = evaluate(
            lm=lm,
            task_dict=task_dict,
            limit=limit,
            cache_requests=cache_requests,
            rewrite_requests_cache=rewrite_requests_cache,
            bootstrap_iters=bootstrap_iters,
            write_out=write_out,
            log_samples=log_samples,
            system_instruction=system_instruction,
            apply_chat_template=apply_chat_template,
            fewshot_as_multiturn=fewshot_as_multiturn,
            verbosity=verbosity,
            retrieval_args=retrieval_args,
        )

    if lm.rank == 0:
        if isinstance(model, str):
            model_name = model
        elif hasattr(model, "config") and hasattr(model.config, "_name_or_path"):
            model_name = model.config._name_or_path
        else:
            model_name = type(model).__name__

        # add info about the model and few shot config
        results["config"] = {
            "model": model_name,
            "model_args": model_args,
        }
        # add more detailed model info if available
        if isinstance(lm, lm_eval.models.huggingface.HFLM):
            results["config"].update(lm.get_model_info())
        # add info about execution
        results["config"].update(
            {
                "batch_size": batch_size,
                "batch_sizes": (
                    list(lm.batch_sizes.values()) if hasattr(lm, "batch_sizes") else []
                ),
                "device": device,
                "use_cache": use_cache,
                "limit": limit,
                "bootstrap_iters": bootstrap_iters,
                "gen_kwargs": gen_kwargs,
                "random_seed": random_seed,
                "numpy_seed": numpy_random_seed,
                "torch_seed": torch_random_seed,
                "fewshot_seed": fewshot_random_seed,
            }
        )
        results["git_hash"] = get_git_commit_hash()
        results["date"] = start_date
        add_env_info(results)  # additional environment info to results
        return results
    else:
        return None


@positional_deprecated
def evaluate(
    lm: "LM",
    task_dict,
    limit: Optional[int] = None,
    cache_requests: bool = False,
    rewrite_requests_cache: bool = False,
    bootstrap_iters: Optional[int] = 100000,
    write_out: bool = False,
    log_samples: bool = True,
    system_instruction: Optional[str] = None,
    apply_chat_template: bool = False,
    fewshot_as_multiturn: bool = False,
    verbosity: str = "INFO",
    retrieval_args: dict = {},
):
    """Instantiate and evaluate a model on a list of tasks.

    :param lm: obj
        Language Model
    :param task_dict: dict[str, Task]
        Dictionary of tasks. Tasks will be taken to have name type(task).config.task .
    :param limit: int, optional
        Limit the number of examples per task (only use this for testing)
    :param bootstrap_iters:
        Number of iterations for bootstrap statistics, used when calculating stderr. Set to 0 for skipping all stderr calculations.
    :param write_out: bool
        If True, write out an example document and model input for checking task integrity
    :param log_samples: bool
        If True, write out all model outputs and documents for per-sample measurement and post-hoc analysis
    :param system_instruction: str
        System instruction to be applied to the prompt
    :param apply_chat_template: bool
        If True, apply chat template to the prompt
    :param fewshot_as_multiturn: bool
        Whether to provide the fewshot examples as a multiturn conversation or a single user turn.
    :return
        Dictionary of results
    """

    eval_logger.setLevel(getattr(logging, f"{verbosity}"))

    # tracks all Instances/requests a model must generate output on.
    requests = defaultdict(list)
    # stores the amount to pad out reqs per req. type so that
    # number of fwd passes per distributed rank is equal
    padding_requests = defaultdict(int)

    # get lists of group hierarchy and each type of request
    task_hierarchy, eval_tasks = get_task_list(task_dict)
    if not log_samples:
        if not all(
            "bypass" not in getattr(task_output.task, "_metric_fn_list", {}).keys()
            for task_output in eval_tasks
        ):
            raise ValueError("log_samples must be True for 'bypass' metric-only tasks")
    
    brute_force_rag_eval = retrieval_args['brute_force_rag_eval']
    # hash the retrieval results first
    if retrieval_args['retrieval_file']:
        logging.info(f"Hashing the retrieval documents once for {len(eval_tasks)} tasks")
        hashed_retrieval_results = hash_retrieval_results(retrieval_args['retrieval_file'], retrieval_args['concat_k'], None, specified_document_id=retrieval_args['specified_document_id'])

    for task_output in eval_tasks:
        task: Task = task_output.task
        limit = get_sample_size(task, limit)
        task.build_all_requests(
            limit=limit,
            rank=lm.rank,
            world_size=lm.world_size,
            cache_requests=cache_requests,
            rewrite_requests_cache=rewrite_requests_cache,
            system_instruction=system_instruction,
            apply_chat_template=apply_chat_template,
            fewshot_as_multiturn=fewshot_as_multiturn,
            lm=lm,
        )
        eval_logger.debug(
            f"Task: {task_output.task_name}; number of requests on this rank: {len(task.instances)}"
        )
        if write_out:
            print_writeout(task)
        # aggregate Instances by LM method requested to get output.
        for instance in task.instances:
            reqtype = instance.request_type
            requests[reqtype].append(instance)

        if lm.world_size > 1:
            instances_rnk = torch.tensor(len(task._instances), device=lm.device)
            gathered_item = (
                lm.accelerator.gather(instances_rnk).cpu().detach().numpy().tolist()
            )
            # "multiple_choice" task types dispatch (several) "loglikelihood" request types
            reqtype = (
                "loglikelihood"
                if task.OUTPUT_TYPE == "multiple_choice"
                else task.OUTPUT_TYPE
            )
            # compute number of pseudo-batches to pad with (FSDP/DDP require even batches among ranks)
            numpad = max(gathered_item) - gathered_item[lm.rank]
            # todo: may not account for padding in cases like SquadV2 which has multiple req types
            padding_requests[reqtype] += numpad
        
        # save inputs for retrieval
        output_dir = retrieval_args['inputs_save_dir']
        os.makedirs(output_dir, exist_ok=True)
        save_file = os.path.join(output_dir, f'{task_output.task_name}.jsonl')
        eval_logger.info(f'Saving inputs for retrieval\n\tTask: {task_output.task_name}\n\tPath {save_file}.')
        if not os.path.exists(save_file) or retrieval_args['overwrite_saved_inputs']:
            with open(save_file, 'w') as fout:
                for instance in task.instances:
                    prompt_end = instance.arguments[0]
                    fout.write(json.dumps({'query': extract_question_from_fewshot_prompt(prompt_end)}) + '\n')
        
        # save answers for analysis
        if retrieval_args['answer_save_dir']:
            answer_save_dir = retrieval_args['answer_save_dir']
            os.makedirs(answer_save_dir, exist_ok=True)
            save_file = os.path.join(answer_save_dir, f'{task_output.task_name}.jsonl')
            eval_logger.info(f'Saving inputs and answers for retrieval\n\tTask: {task_output.task_name}\n\tPath {save_file}.')
            if not os.path.exists(save_file) or retrieval_args['overwrite_saved_inputs']:
                with open(save_file, 'w') as fout:
                    for instance in task.instances:
                        prompt_end = instance.arguments[0]
                        if instance.request_type == 'loglikelihood':
                            answer = extract_answer_from_loglikelihood_task(instance.arguments[0], instance.arguments[1])
                        else:
                            if 'answer' in instance.doc:
                                answer = instance.doc['answer']
                            elif 'answers' in instance.doc:
                                answer = instance.doc['answers']
                            else:
                                raise AttributeError
                        fout.write(json.dumps({'query': prompt_end, 'answer': answer}) + '\n')
        
        # skip evaluation is save_inputs_only is True
        if retrieval_args['save_inputs_only']:
            continue
        
        # prepend retrieved documents if any
        logging.info(f'Retrieval arguments: {retrieval_args}')
    
        if not brute_force_rag_eval and (retrieval_args['retrieval_file'] or retrieval_args['retrieval_dir']):
            if retrieval_args['retrieval_dir']:
                subtask_retrieval_file = f"{retrieval_args['retrieval_dir']}/{task_output.task_name}_retrieved_results.jsonl"
                assert os.path.exists(subtask_retrieval_file),f"retrieval path does not exist: {subtask_retrieval_file}"
                hashed_retrieval_results = hash_retrieval_results(subtask_retrieval_file, retrieval_args['concat_k'], task, specified_document_id=retrieval_args['specified_document_id'])
            #assert len(task.instances) == len(hashed_retrieval_results), f'length mismatch between task data ({len(task.instances)}) and retrieval data ({len(hashed_retrieval_results)})'
            
            for i, instance in enumerate(task.instances):
                prompt_end = instance.arguments[0]
                query = extract_question_from_fewshot_prompt(prompt_end)
                if query in hashed_retrieval_results:
                    prompt_retrieval = hashed_retrieval_results[query]
                elif task._config.description in query and query.replace(task._config.description, '') in hashed_retrieval_results:
                    prompt_retrieval = hashed_retrieval_results[query.replace(task._config.description, '')]
                elif task._config.description.replace('\n\n', '\n') in query and query.replace(task._config.description.replace('\n\n', '\n'), '') in hashed_retrieval_results:
                    prompt_retrieval = hashed_retrieval_results[query.replace(task._config.description.replace('\n\n', '\n'), '')]
                elif task._config.description + query in hashed_retrieval_results:
                    prompt_retrieval = hashed_retrieval_results[task._config.description + query]
                elif task._config.description.replace('\n\n', '\n') + query in hashed_retrieval_results:
                    prompt_retrieval = hashed_retrieval_results[task._config.description.replace('\n\n', '\n') + query]
                else:
                    eval_logger.info(hashed_retrieval_results.keys())
                    eval_logger.info('\n\n\Query: Not found\n\n\n')
                    eval_logger.info(query)
                    eval_logger.info(task._config.description.replace('\n\n', '\n') in query)
                    eval_logger.info(query.replace(task._config.description.replace('\n\n', '\n'), ''))
                    import pdb; pdb.set_trace()

                prompt = prompt_retrieval + prompt_end
                if retrieval_args['additional_system_prompt']:
                    prompt = prompt_retrieval + '\n\n' + retrieval_args['additional_system_prompt'] + prompt
                if i == 0:
                    print(f"Sample prompt:\n{prompt}")
                task.instances[i].arguments = (prompt, *instance.arguments[1:])
        
        elif retrieval_args['additional_system_prompt']:
            for i, instance in enumerate(task.instances):
                prompt = retrieval_args['additional_system_prompt'] + instance.arguments[0]
                task.instances[i].arguments = (prompt, *instance.arguments[1:])

        if lm.world_size > 1:
            instances_rnk = torch.tensor(len(task._instances), device=lm.device)
            gathered_item = (
                lm.accelerator.gather(instances_rnk).cpu().detach().numpy().tolist()
            )
            # "multiple_choice" task types dispatch (several) "loglikelihood" request types
            reqtype = (
                "loglikelihood"
                if task.OUTPUT_TYPE == "multiple_choice"
                else task.OUTPUT_TYPE
            )
            # compute number of pseudo-batches to pad with (FSDP/DDP require even batches among ranks)
            numpad = max(gathered_item) - gathered_item[lm.rank]
            # todo: may not account for padding in cases like SquadV2 which has multiple req types
            padding_requests[reqtype] += numpad

    eval_logger.info('\n\n\nRulin\n\n\n')
    # Skip evaluation is save_inputs_only is True
    if retrieval_args['save_inputs_only']:
        logging.info("Skipping evaluation because save_inputs_only is set to True...")
        return {}

    ### Run LM on inputs, get all outputs ###
    # execute each type of request
    if brute_force_rag_eval:
        retrieval_data = hash_retrieval_results(retrieval_args['retrieval_file'], hash_all=True)
        
    eval_logger.info('\n\n\nRulin\n\n\n')
    for reqtype, reqs in requests.items():
        eval_logger.info(f"Running {reqtype} requests")
        # create `K` copies of each request `req` based off `K = req.repeats`
        cloned_reqs = []
        for req in reqs:
            cloned_reqs.extend([req] * req.repeats)

        if (lm.world_size > 1) and (padding_requests[reqtype] > 0):
            for _ in range(padding_requests[reqtype]):
                cloned_reqs.extend([req] * req.repeats)

        # run requests through model
        if brute_force_rag_eval:
            resps = []
            all_local_reqs = []
            num_documents = 100
            for req in reqs:
                prompt_end = req.arguments[0]
                raw_query = extract_question_from_fewshot_prompt(prompt_end)
                documents = retrieval_data[raw_query][:num_documents]
                local_cloned_reqs = [copy.deepcopy(req) for _ in range(num_documents)]
                for i in range(num_documents):
                    prompt_retrieval = documents[i]
                    new_prompt_end = prompt_retrieval + '\n\n' + retrieval_args['additional_system_prompt'] + prompt_end
                    local_cloned_reqs[i].arguments = (new_prompt_end, *req.arguments[1:])
                    
                local_resps = getattr(lm, reqtype)(local_cloned_reqs)
                resps.extend(local_resps)
                # keep the resps that get the final answer right
                # also need to note down all the gen results with different docs
                for x, local_req in zip(local_resps, local_cloned_reqs):
                    local_req.resps.append(x)
                all_local_reqs.extend(local_cloned_reqs)
        else:
            eval_logger.info('\n\n\nRulin2\n\n\n')
            resps = getattr(lm, reqtype)(cloned_reqs)

            # put responses from model into a list of length K for each request.
            for x, req in zip(resps, cloned_reqs):
                req.resps.append(x)

        if lm.world_size > 1:
            lm.accelerator.wait_for_everyone()

    RANK = lm.rank
    WORLD_SIZE = lm.world_size
    ### Postprocess outputs ###
    # TODO: del model here, maybe (idea: allow user to specify device of e.g. reward model separately)
    for task_output in eval_tasks:
        task = task_output.task
        task.apply_filters()

        ### Collect values of metrics on all datapoints ###
        # # unpack results and sort back in order and return control to Task
        # TODO: make it possible to use a different metric per filter
        # Pre-process task.instances to group by doc_id
        instances_by_doc_id = defaultdict(list)
        if brute_force_rag_eval:
            for instance in all_local_reqs:
                instances_by_doc_id[instance.doc_id].append(instance)
        else:
            for instance in task.instances:
                instances_by_doc_id[instance.doc_id].append(instance)
        # Sort instances within each group
        for instances in instances_by_doc_id.values():
            instances.sort(key=lambda x: x.idx)
        # iterate over different filters used
        for filter_key in task.instances[0].filtered_resps.keys():
            doc_iterator = task.doc_iterator(
                rank=RANK, limit=limit, world_size=WORLD_SIZE
            )
            for doc_id, doc in doc_iterator:
                requests = instances_by_doc_id[doc_id]
                metrics = task.process_results(
                    doc, [req.filtered_resps[filter_key] for req in requests]
                )
                if log_samples:
                    target = task.doc_to_target(doc)
                    example = {
                        "doc_id": doc_id,
                        "doc": doc,
                        "target": target,
                        "arguments": [req.args for req in requests],
                        "resps": [req.resps for req in requests],
                        "filtered_resps": [
                            req.filtered_resps[filter_key] for req in requests
                        ],
                        "doc_hash": hash_string(
                            json.dumps(
                                requests[0].doc,
                                indent=2,
                                default=handle_non_serializable,
                                ensure_ascii=False,
                            )
                        ),
                        "prompt_hash": hash_string(requests[0].arguments[0]),
                        "target_hash": hash_string(str(target)),
                    }
                    example.update(metrics)
                    task_output.logged_samples.append(example)
                for metric, value in metrics.items():
                    task_output.sample_metrics[(metric, filter_key)].append(value)

    if WORLD_SIZE > 1:
        # if multigpu, then gather data across all ranks to rank 0
        # first gather logged samples across all ranks
        for task_output in eval_tasks:
            if log_samples:
                # for task_name, task_samples in list(samples.items()):
                full_samples = [None] * WORLD_SIZE if RANK == 0 else None
                torch.distributed.gather_object(
                    obj=task_output.logged_samples,
                    object_gather_list=full_samples,
                    dst=0,
                )

                if RANK == 0:
                    task_output.logged_samples = list(
                        itertools.chain.from_iterable(full_samples)
                    )

            # then collect metrics across all ranks
            for metrics in task_output.sample_metrics:
                metric_list = [None] * WORLD_SIZE if RANK == 0 else None
                torch.distributed.gather_object(
                    obj=task_output.sample_metrics[metrics],
                    object_gather_list=metric_list,
                    dst=0,
                )
                if RANK == 0:
                    task_output.sample_metrics[metrics] = list(
                        itertools.chain.from_iterable(metric_list)
                    )

    if RANK == 0:
        ### Aggregate results over all datapoints ###
        # aggregate results ; run bootstrap CIs
        for task_output in eval_tasks:
            task_output.calculate_aggregate_metric(bootstrap_iters=bootstrap_iters)
        (
            results,
            samples,
            configs,
            versions,
            num_fewshot,
            higher_is_better,
        ) = consolidate_results(eval_tasks)

        ### Calculate group metrics ###
        if bool(results):
            for group, task_list in reversed(task_hierarchy.items()):
                if len(task_list) == 0:
                    # task_hierarchy entries are either
                    # `group_name: [subtask1, subtask2, ...]`
                    # or `task_name: []`.
                    # we only want to operate on groups here.
                    continue

                # collect all higher_is_better values for metrics
                # in the group's subtasks.
                # TODO: clean this up ; unify with the below metric_list loop?
                _higher_is_better = {}
                for task in task_list:
                    for m, h in higher_is_better[task].items():
                        if m not in _higher_is_better.keys():
                            _higher_is_better[m] = h
                    if (
                        m in _higher_is_better
                        and _higher_is_better[m] is not None
                        and _higher_is_better[m] != h
                    ):
                        eval_logger.warning(
                            f"Higher_is_better values for metric {m} in group {group} are not consistent. Defaulting to None."
                        )
                        _higher_is_better[m] = None
                higher_is_better[group] = _higher_is_better

                # collect all metric keys used by a subtask in the group.
                metric_list = list(
                    {
                        key
                        for task in task_list
                        for key in results[task].keys()
                        if "_stderr" not in key and key not in ["alias", "samples"]
                    }
                )
                for metric in metric_list:
                    stderr = "_stderr,".join(metric.split(","))

                    # gather metrics, sizes, and stderrs from subtasks
                    metrics = [
                        results[task][metric]
                        for task in task_list
                        if metric in results[task]
                    ]  # TODO: copy?
                    stderrs = [
                        results[task][stderr]
                        for task in task_list
                        if stderr in results[task]
                    ]
                    sizes = [
                        results[task]["samples"]
                        for task in task_list
                        if metric in results[task]
                    ]

                    # compute group's pooled metric and stderr
                    results[group][
                        metric
                    ] = lm_eval.api.metrics.aggregate_subtask_metrics(metrics, sizes)
                    # TODO: calculate grouped metric using aggregation fn
                    if "N/A" in stderrs:
                        results[group][stderr] = "N/A"
                    else:
                        results[group][
                            stderr
                        ] = lm_eval.api.metrics.pooled_sample_stderr(stderrs, sizes)
                        # TODO: allow GroupConfigs to choose which variance formula is used, for back-compatibility
                        # To use the old (likely incorrect) variance formula, comment out the above and uncomment this line:
                        # results[group][stderr] = lm_eval.api.metrics.combined_sample_stderr(stderrs, sizes, metrics=metrics)

                    results[group]["samples"] = sum(sizes)

        results_agg = defaultdict(dict)
        groups_agg = defaultdict(dict)
        all_tasks_list = list(task_hierarchy.keys())
        while True:
            add_tasks_list = list(k for k in results_agg.keys())
            left_tasks_list = sorted(list(set(all_tasks_list) - set(add_tasks_list)))
            if len(left_tasks_list) == 0:
                break

            _task_hierarchy = {
                k: v for k, v in task_hierarchy.items() if k in left_tasks_list
            }
            _results_agg, _groups_agg = prepare_print_tasks(_task_hierarchy, results)

            results_agg = {**results_agg, **_results_agg}
            groups_agg = {**groups_agg, **_groups_agg}

        for group_name, task_list in task_hierarchy.items():
            if task_list:
                num_fewshot[group_name] = num_fewshot[
                    task_list[0]
                ]  # TODO: validate this

        results_dict = {
            "results": dict(results_agg.items()),
            **({"groups": dict(groups_agg.items())} if bool(groups_agg) else {}),
            "group_subtasks": dict(reversed(task_hierarchy.items())),
            "configs": dict(sorted(configs.items())),
            "versions": dict(sorted(versions.items())),
            "n-shot": dict(sorted(num_fewshot.items())),
            "n-doc": {k: retrieval_args['concat_k'] for k in dict(sorted(num_fewshot.items())).keys()},
            "higher_is_better": dict(sorted(higher_is_better.items())),
            "n-samples": {
                task_output.task_name: {
                    "original": len(task_output.task.eval_docs),
                    "effective": min(
                        limit if limit else len(task_output.task.eval_docs),
                        len(task_output.task.eval_docs),
                    ),
                }
                for task_output in eval_tasks
            },
        }
        if log_samples:
            results_dict["samples"] = dict(samples)

        return results_dict

    else:
        return None


def request_caching_arg_to_dict(cache_requests: str) -> dict:
    request_caching_args = {
        "cache_requests": cache_requests in {"true", "refresh"},
        "rewrite_requests_cache": cache_requests == "refresh",
        "delete_requests_cache": cache_requests == "delete",
    }

    return request_caching_args


def load_jsonlines(file):
    with jsonlines.open(file, 'r') as jsonl_f:
        lst = [obj for obj in jsonl_f]
    return lst


def extract_question_from_fewshot_prompt(prompt):
    """
    Extract the 0-shot question from fewshot example. 
    We split by '\n\n' because it is the default fewshot delimiter.
    Please make sure all future tasks use '\n\n' as the fewshot delimiter.
    """
    return prompt.split('\n\n')[-1]


def hash_retrieval_results(
        test_jsonl_with_retrieval: str = "",
        concat_k: int = 1,
        task=None,
        hash_all: bool = False,
        specified_document_id: int = None,
    ):
        hashed_results = {}
        qa_data = load_jsonlines(test_jsonl_with_retrieval)
        for data in qa_data:
            assert 'question' or 'raw_query' or 'query' in data
            if 'raw_query' in data or 'query' in data:
                raw_query = data['raw_query'] if 'raw_query' in data else data['query']
                # hotfix: remove system prompt in the original query based on '\n\n' to align the extracted query in the few-shot setting.
                raw_query = extract_question_from_fewshot_prompt(raw_query)
            else:
                query = data['question']
                raw_query = task._config.description + task.doc_to_text({'question': query}) 

            if hash_all:
                hashed_results[raw_query] = [data['ctxs'][i]["retrieval text"] for i in range(len(data['ctxs']))]
            elif specified_document_id is not None:
                ctx = data['ctxs'][specified_document_id]["retrieval text"]
                hashed_results[raw_query] = ctx
            else:
                k_ctx = ''
                for i in range(concat_k):
                    try:
                        # todo: unify the key name
                        k_ctx = data['ctxs'][i]["retrieval text"] + k_ctx if "retrieval text" in data['ctxs'][i].keys() else data['ctxs'][i]["text"] + k_ctx
                    except:
                        print(f"No enough documents to prepend! Added {i} documents only.")
                
                try:
                    assert raw_query not in hashed_results.keys() or k_ctx == hashed_results[raw_query]
                except:
                    print(f"\n\nMismatched:\nQuery:{raw_query}\nHashed Doc:{hashed_results[raw_query]}\nNew Doc:{k_ctx}")
                #assert raw_query not in hashed_results, f'query already in hashed results: {raw_query}'
                hashed_results[raw_query] = k_ctx
        return hashed_results


def extract_answer_from_loglikelihood_task(input_text, answer_label):
    # Split the input into question and answer parts
    parts = input_text.split('Answer:')
    question_part = parts[0]

    # Normalize the answer label to remove extra spaces
    answer_label = answer_label.strip()

    # Split the question part further to isolate answer choices
    answer_choices = question_part.split('\n')
    answer_choices = [choice.strip() for choice in answer_choices if choice.strip()]

    # Find the answer choice that matches the answer label
    for choice in answer_choices:
        if choice.startswith(answer_label):
            return choice

    return "Answer not found"
