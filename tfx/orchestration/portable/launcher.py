# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This module defines a generic Launcher for all TFleX nodes."""

from typing import Any, Dict, List, Optional, Text, Type, TypeVar

from absl import logging
import attr
from tfx import types
from tfx.dsl.io import fileio
from tfx.orchestration import metadata
from tfx.orchestration.portable import base_driver_operator
from tfx.orchestration.portable import base_executor_operator
from tfx.orchestration.portable import cache_utils
from tfx.orchestration.portable import data_types
from tfx.orchestration.portable import docker_executor_operator
from tfx.orchestration.portable import execution_publish_utils
from tfx.orchestration.portable import importer_node_handler
from tfx.orchestration.portable import inputs_utils
from tfx.orchestration.portable import outputs_utils
from tfx.orchestration.portable import python_driver_operator
from tfx.orchestration.portable import python_executor_operator
from tfx.orchestration.portable import resolver_node_handler
from tfx.orchestration.portable.mlmd import context_lib
from tfx.proto.orchestration import driver_output_pb2
from tfx.proto.orchestration import executable_spec_pb2
from tfx.proto.orchestration import execution_result_pb2
from tfx.proto.orchestration import pipeline_pb2

from google.protobuf import message
from ml_metadata.proto import metadata_store_pb2
# Subclasses of BaseExecutorOperator
ExecutorOperator = TypeVar(
    'ExecutorOperator', bound=base_executor_operator.BaseExecutorOperator)

# Subclasses of BaseDriverOperator
DriverOperator = TypeVar(
    'DriverOperator', bound=base_driver_operator.BaseDriverOperator)

DEFAULT_EXECUTOR_OPERATORS = {
    executable_spec_pb2.PythonClassExecutableSpec:
        python_executor_operator.PythonExecutorOperator,
    executable_spec_pb2.ContainerExecutableSpec:
        docker_executor_operator.DockerExecutorOperator
}

DEFAULT_DRIVER_OPERATORS = {
    executable_spec_pb2.PythonClassExecutableSpec:
        python_driver_operator.PythonDriverOperator
}

# LINT.IfChange
_SYSTEM_NODE_HANDLERS = {
    'tfx.dsl.components.common.importer.Importer':
        importer_node_handler.ImporterNodeHandler,
    'tfx.dsl.components.common.resolver.Resolver':
        resolver_node_handler.ResolverNodeHandler,
    # TODO(b/177457236): Remove support for the following after release.
    'tfx.dsl.components.common.importer_node.ImporterNode':
        importer_node_handler.ImporterNodeHandler,
    'tfx.dsl.components.common.resolver_node.ResolverNode':
        resolver_node_handler.ResolverNodeHandler,
}
# LINT.ThenChange(Internal system node list)


# TODO(b/165359991): Restore 'auto_attribs=True' once we drop Python3.5 support.
@attr.s
class _PrepareExecutionResult:
  """A wrapper class using as the return value of _prepare_execution()."""

  # The information used by executor operators.
  execution_info = attr.ib(type=data_types.ExecutionInfo, default=None)
  # The Execution registered in MLMD.
  execution_metadata = attr.ib(type=metadata_store_pb2.Execution, default=None)
  # Contexts of the execution, usually used by Publisher.
  contexts = attr.ib(type=List[metadata_store_pb2.Context], default=None)
  # TODO(b/156126088): Update the following documentation when this bug is
  # closed.
  # Whether an execution is needed. An execution is not needed when:
  # 1) Not all the required input are ready.
  # 2) The input value doesn't meet the driver's requirement.
  # 3) Cache result is used.
  is_execution_needed = attr.ib(type=bool, default=False)


class _ExecutionFailedError(Exception):
  """An internal error to carry ExecutorOutput when it is raised."""

  def __init__(self, err_msg: str,
               executor_output: execution_result_pb2.ExecutorOutput):
    super(_ExecutionFailedError, self).__init__(err_msg)
    self._executor_output = executor_output

  @property
  def executor_output(self):
    return self._executor_output


class Launcher(object):
  """Launcher is the main entrance of nodes in TFleX.

     It handles TFX internal details like artifact resolving, execution
     triggering and result publishing.
  """

  def __init__(
      self,
      pipeline_node: pipeline_pb2.PipelineNode,
      mlmd_connection: metadata.Metadata,
      pipeline_info: pipeline_pb2.PipelineInfo,
      pipeline_runtime_spec: pipeline_pb2.PipelineRuntimeSpec,
      executor_spec: Optional[message.Message] = None,
      custom_driver_spec: Optional[message.Message] = None,
      platform_config: Optional[message.Message] = None,
      custom_executor_operators: Optional[Dict[Any,
                                               Type[ExecutorOperator]]] = None,
      custom_driver_operators: Optional[Dict[Any,
                                             Type[DriverOperator]]] = None):
    """Initializes a Launcher.

    Args:
      pipeline_node: The specification of the node that this launcher lauches.
      mlmd_connection: ML metadata connection.
      pipeline_info: The information of the pipeline that this node runs in.
      pipeline_runtime_spec: The runtime information of the pipeline that this
        node runs in.
      executor_spec: Specification for the executor of the node. This is
        expected for all components nodes. This will be used to determine the
        specific ExecutorOperator class to be used to execute and will be passed
        into ExecutorOperator.
      custom_driver_spec: Specification for custom driver. This is expected only
        for advanced use cases.
      platform_config: Platform config that will be used as auxiliary info of
        the node execution. This will be passed to ExecutorOperator along with
        the `executor_spec`.
      custom_executor_operators: a map of ExecutableSpec to its
        ExecutorOperation implementation.
      custom_driver_operators: a map of ExecutableSpec to its DriverOperator
        implementation.

    Raises:
      ValueError: when component and component_config are not launchable by the
      launcher.
    """
    self._pipeline_node = pipeline_node
    self._mlmd_connection = mlmd_connection
    self._pipeline_info = pipeline_info
    self._pipeline_runtime_spec = pipeline_runtime_spec
    self._executor_spec = executor_spec
    self._executor_operators = {}
    self._executor_operators.update(DEFAULT_EXECUTOR_OPERATORS)
    self._executor_operators.update(custom_executor_operators or {})
    self._driver_operators = {}
    self._driver_operators.update(DEFAULT_DRIVER_OPERATORS)
    self._driver_operators.update(custom_driver_operators or {})

    self._executor_operator = None
    if executor_spec:
      self._executor_operator = self._executor_operators[type(executor_spec)](
          executor_spec, platform_config)
    self._output_resolver = outputs_utils.OutputsResolver(
        pipeline_node=self._pipeline_node,
        pipeline_info=self._pipeline_info,
        pipeline_runtime_spec=self._pipeline_runtime_spec)

    self._driver_operator = None
    if custom_driver_spec:
      self._driver_operator = self._driver_operators[type(custom_driver_spec)](
          custom_driver_spec, self._mlmd_connection)

    system_node_handler_class = _SYSTEM_NODE_HANDLERS.get(
        self._pipeline_node.node_info.type.name)
    self._system_node_handler = None
    if system_node_handler_class:
      self._system_node_handler = system_node_handler_class()

    assert bool(self._executor_operator) or bool(self._system_node_handler), \
        'A node must be system node or have an executor.'

  def _prepare_execution(self) -> _PrepareExecutionResult:
    """Prepares inputs, outputs and execution properties for actual execution."""
    # TODO(b/150979622): handle the edge case that the component get evicted
    # between successful pushlish and stateful working dir being clean up.
    # Otherwise following retries will keep failing because of duplicate
    # publishes.
    with self._mlmd_connection as m:
      # 1.Prepares all contexts.
      contexts = context_lib.prepare_contexts(
          metadata_handler=m, node_contexts=self._pipeline_node.contexts)

      # 2. Resolves inputs an execution properties.
      exec_properties = inputs_utils.resolve_parameters(
          node_parameters=self._pipeline_node.parameters)
      input_artifacts = inputs_utils.resolve_input_artifacts(
          metadata_handler=m, node_inputs=self._pipeline_node.inputs)
      # 3. If not all required inputs are met. Return ExecutionInfo with
      # is_execution_needed being false. No publish will happen so down stream
      # nodes won't be triggered.
      if input_artifacts is None:
        logging.info('No all required input are ready, abandoning execution.')
        return _PrepareExecutionResult(
            execution_info=data_types.ExecutionInfo(),
            contexts=contexts,
            is_execution_needed=False)

      # 4. Registers execution in metadata.
      execution = execution_publish_utils.register_execution(
          metadata_handler=m,
          execution_type=self._pipeline_node.node_info.type,
          contexts=contexts,
          input_artifacts=input_artifacts,
          exec_properties=exec_properties)

      # 5. Resolve output
      output_artifacts = self._output_resolver.generate_output_artifacts(
          execution.id)

    # If there is a custom driver, runs it.
    if self._driver_operator:
      driver_output = self._driver_operator.run_driver(
          data_types.ExecutionInfo(
              input_dict=input_artifacts,
              output_dict=output_artifacts,
              exec_properties=exec_properties,
              execution_output_uri=self._output_resolver.get_driver_output_uri(
              )))
      self._update_with_driver_output(driver_output, exec_properties,
                                      output_artifacts)

    # We reconnect to MLMD here because the custom driver closes MLMD connection
    # on returning.
    with self._mlmd_connection as m:
      # 6. Check cached result
      cache_context = cache_utils.get_cache_context(
          metadata_handler=m,
          pipeline_node=self._pipeline_node,
          pipeline_info=self._pipeline_info,
          executor_spec=self._executor_spec,
          input_artifacts=input_artifacts,
          output_artifacts=output_artifacts,
          parameters=exec_properties)
      contexts.append(cache_context)
      cached_outputs = cache_utils.get_cached_outputs(
          metadata_handler=m, cache_context=cache_context)

      # 7. Should cache be used?
      if (self._pipeline_node.execution_options.caching_options.enable_cache and
          cached_outputs):
        # Publishes cache result
        execution_publish_utils.publish_cached_execution(
            metadata_handler=m,
            contexts=contexts,
            execution_id=execution.id,
            output_artifacts=cached_outputs)
        logging.info('An cached execusion %d is used.', execution.id)
        return _PrepareExecutionResult(
            execution_info=data_types.ExecutionInfo(execution_id=execution.id),
            execution_metadata=execution,
            contexts=contexts,
            is_execution_needed=False)

      pipeline_run_id = (
          self._pipeline_runtime_spec.pipeline_run_id.field_value.string_value)

      # 8. Going to trigger executor.
      logging.info('Going to run a new execution %d', execution.id)
      return _PrepareExecutionResult(
          execution_info=data_types.ExecutionInfo(
              execution_id=execution.id,
              input_dict=input_artifacts,
              output_dict=output_artifacts,
              exec_properties=exec_properties,
              execution_output_uri=self._output_resolver
              .get_executor_output_uri(execution.id),
              stateful_working_dir=(
                  self._output_resolver.get_stateful_working_directory()),
              tmp_dir=self._output_resolver.make_tmp_dir(execution.id),
              pipeline_node=self._pipeline_node,
              pipeline_info=self._pipeline_info,
              pipeline_run_id=pipeline_run_id),
          execution_metadata=execution,
          contexts=contexts,
          is_execution_needed=True)

  def _run_executor(
      self, execution_info: data_types.ExecutionInfo
  ) -> execution_result_pb2.ExecutorOutput:
    """Executes underlying component implementation."""

    logging.info('Going to run a new execution: %s', execution_info)

    outputs_utils.make_output_dirs(execution_info.output_dict)
    try:
      executor_output = self._executor_operator.run_executor(execution_info)
      code = executor_output.execution_result.code
      if code != 0:
        result_message = executor_output.execution_result.result_message
        err = (f'Execution {execution_info.execution_id} '
               f'failed with error code {code} and '
               f'error message {result_message}')
        logging.error(err)
        raise _ExecutionFailedError(err, executor_output)
      return executor_output
    except Exception:  # pylint: disable=broad-except
      outputs_utils.remove_output_dirs(execution_info.output_dict)
      raise

  def _publish_successful_execution(
      self, execution_id: int, contexts: List[metadata_store_pb2.Context],
      output_dict: Dict[Text, List[types.Artifact]],
      executor_output: execution_result_pb2.ExecutorOutput) -> None:
    """Publishes succeeded execution result to ml metadata."""
    with self._mlmd_connection as m:
      execution_publish_utils.publish_succeeded_execution(
          metadata_handler=m,
          execution_id=execution_id,
          contexts=contexts,
          output_artifacts=output_dict,
          executor_output=executor_output)

  def _publish_failed_execution(
      self,
      execution_id: int,
      contexts: List[metadata_store_pb2.Context],
      executor_output: Optional[execution_result_pb2.ExecutorOutput] = None
  ) -> None:
    """Publishes failed execution to ml metadata."""
    with self._mlmd_connection as m:
      execution_publish_utils.publish_failed_execution(
          metadata_handler=m,
          execution_id=execution_id,
          contexts=contexts,
          executor_output=executor_output)

  def _clean_up_stateless_execution_info(
      self, execution_info: data_types.ExecutionInfo):
    logging.info('Cleaning up stateless execution info.')
    # Clean up tmp dir
    fileio.rmtree(execution_info.tmp_dir)

  def _clean_up_stateful_execution_info(
      self, execution_info: data_types.ExecutionInfo):
    """Post execution clean up."""
    logging.info('Cleaning up stateful execution info.')
    outputs_utils.remove_stateful_working_dir(
        execution_info.stateful_working_dir)

  def _update_with_driver_output(self,
                                 driver_output: driver_output_pb2.DriverOutput,
                                 exec_properties: Dict[Text, Any],
                                 output_dict: Dict[Text, List[types.Artifact]]):
    """Updates output_dict with driver output."""
    for key, artifact_list in driver_output.output_artifacts.items():
      python_artifact_list = []
      # We assume the origial output dict must include at least one output
      # artifact and all output artifact shared the same type.
      artifact_type = output_dict[key][0].artifact_type
      for proto_artifact in artifact_list.artifacts:
        python_artifact = types.Artifact(artifact_type)
        python_artifact.set_mlmd_artifact(proto_artifact)
        python_artifact_list.append(python_artifact)
      output_dict[key] = python_artifact_list

    for key, value in driver_output.exec_properties.items():
      exec_properties[key] = getattr(value, value.WhichOneof('value'))

  def launch(self) -> Optional[metadata_store_pb2.Execution]:
    """Executes the component, includes driver, executor and publisher.

    Returns:
      The metadata of this execution that is registered in MLMD. It can be None
      if the driver decides not to run the execution.

    Raises:
      Exception: If the executor fails.
    """
    logging.info('Running launcher for %s', self._pipeline_node)
    if self._system_node_handler:
      # If this is a system node, runs it and directly return.
      return self._system_node_handler.run(self._mlmd_connection,
                                           self._pipeline_node,
                                           self._pipeline_info,
                                           self._pipeline_runtime_spec)

    # Runs as a normal node.
    prepare_execution_result = self._prepare_execution()
    (execution_info, contexts,
     is_execution_needed) = (prepare_execution_result.execution_info,
                             prepare_execution_result.contexts,
                             prepare_execution_result.is_execution_needed)
    if is_execution_needed:
      try:
        executor_output = self._run_executor(execution_info)
      except Exception as e:  # pylint: disable=broad-except
        execution_output = (
            e.executor_output if isinstance(e, _ExecutionFailedError) else None)
        self._publish_failed_execution(execution_info.execution_id, contexts,
                                       execution_output)
        logging.error('Execution %d failed.', execution_info.execution_id)
        raise
      finally:
        self._clean_up_stateless_execution_info(execution_info)

      logging.info('Execution %d succeeded.', execution_info.execution_id)
      self._clean_up_stateful_execution_info(execution_info)

      # TODO(b/182316162): Unify publisher handing so that post-execution
      # artifact logic is more cleanly handled.
      outputs_utils.tag_executor_output_with_version(
          executor_output)

      logging.info('Publishing output artifacts %s for execution %s',
                   execution_info.output_dict, execution_info.execution_id)
      self._publish_successful_execution(execution_info.execution_id, contexts,
                                         execution_info.output_dict,
                                         executor_output)
    return prepare_execution_result.execution_metadata
