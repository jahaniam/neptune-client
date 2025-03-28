#
# Copyright (c) 2022, Neptune Labs Sp. z o.o.
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
#
__all__ = ["Model"]

import os
from typing import (
    Iterable,
    Optional,
)

from neptune.attributes.constants import SYSTEM_NAME_ATTRIBUTE_PATH
from neptune.common.exceptions import NeptuneException
from neptune.envs import CONNECTION_MODE
from neptune.exceptions import (
    InactiveModelException,
    NeedExistingModelForReadOnlyMode,
    NeptuneMissingRequiredInitParameter,
    NeptuneModelKeyAlreadyExistsError,
    NeptuneObjectCreationConflict,
)
from neptune.internal.backends.api_model import ApiExperiment
from neptune.internal.backends.nql import (
    NQLAggregator,
    NQLAttributeOperator,
    NQLAttributeType,
    NQLQueryAggregate,
    NQLQueryAttribute,
)
from neptune.internal.backgroud_job_list import BackgroundJobList
from neptune.internal.container_type import ContainerType
from neptune.internal.id_formats import QualifiedName
from neptune.internal.init.parameters import (
    DEFAULT_FLUSH_PERIOD,
    DEFAULT_NAME,
    OFFLINE_PROJECT_QUALIFIED_NAME,
)
from neptune.internal.state import ContainerState
from neptune.internal.utils import verify_type
from neptune.internal.utils.ping_background_job import PingBackgroundJob
from neptune.metadata_containers import MetadataContainer
from neptune.metadata_containers.metadata_containers_table import Table
from neptune.types.mode import Mode


class Model(MetadataContainer):
    """A class for managing a Neptune model and retrieving information from it.

    You may also want to check `Model docs page`_.

    .. _Model docs page:
       https://docs.neptune.ai/api/model
    """

    container_type = ContainerType.MODEL

    def __init__(
        self,
        with_id: Optional[str] = None,
        *,
        name: Optional[str] = None,
        key: Optional[str] = None,
        project: Optional[str] = None,
        api_token: Optional[str] = None,
        mode: Optional[str] = None,
        flush_period: float = DEFAULT_FLUSH_PERIOD,
        proxies: Optional[dict] = None,
    ):
        verify_type("with_id", with_id, (str, type(None)))
        verify_type("name", name, (str, type(None)))
        verify_type("key", key, (str, type(None)))
        verify_type("project", project, (str, type(None)))
        verify_type("mode", mode, (str, type(None)))

        self._key: Optional[str] = key
        self._with_id: Optional[str] = with_id
        self._name: Optional[str] = DEFAULT_NAME if with_id is None and name is None else name

        # make mode proper Enum instead of string
        mode = Mode(mode or os.getenv(CONNECTION_MODE) or Mode.ASYNC.value)

        if mode == Mode.OFFLINE:
            raise NeptuneException("Model can't be initialized in OFFLINE mode")

        if mode == Mode.DEBUG:
            project = OFFLINE_PROJECT_QUALIFIED_NAME

        super().__init__(project=project, api_token=api_token, mode=mode, flush_period=flush_period, proxies=proxies)

    def _get_or_create_api_object(self) -> ApiExperiment:
        project_workspace = self._project_api_object.workspace
        project_name = self._project_api_object.name
        project_qualified_name = f"{project_workspace}/{project_name}"

        if self._with_id is not None:
            # with_id (resume existing model) has priority over key (creating a new model)
            #  additional creation parameters (e.g. name) are simply ignored in this scenario
            return self._backend.get_metadata_container(
                container_id=QualifiedName(project_qualified_name + "/" + self._with_id),
                expected_container_type=self.container_type,
            )
        elif self._key is not None:
            if self._mode == Mode.READ_ONLY:
                raise NeedExistingModelForReadOnlyMode()

            try:
                return self._backend.create_model(project_id=self._project_api_object.id, key=self._key)
            except NeptuneObjectCreationConflict as e:
                base_url = self._backend.get_display_address()
                raise NeptuneModelKeyAlreadyExistsError(
                    model_key=self._key,
                    models_tab_url=f"{base_url}/{project_workspace}/{project_name}/models",
                ) from e
        else:
            raise NeptuneMissingRequiredInitParameter(
                parameter_name="key",
                called_function="init_model",
            )

    def _prepare_background_jobs(self) -> BackgroundJobList:
        return BackgroundJobList([PingBackgroundJob()])

    def _write_initial_attributes(self):
        if self._name is not None:
            self[SYSTEM_NAME_ATTRIBUTE_PATH] = self._name

    def _raise_if_stopped(self):
        if self._state == ContainerState.STOPPED:
            raise InactiveModelException(label=self._sys_id)

    def get_url(self) -> str:
        """Returns the URL that can be accessed within the browser"""
        return self._backend.get_model_url(
            model_id=self._id,
            workspace=self._workspace,
            project_name=self._project_name,
            sys_id=self._sys_id,
        )

    def fetch_model_versions_table(self, *, columns: Optional[Iterable[str]] = None) -> Table:
        """Retrieve all versions of the given model.

        Args:
            columns: Names of columns to include in the table, as a list of namespace or field names.
                The Neptune ID ("sys/id") is included automatically.
                Examples:
                    Fields: `["params/lr", "params/batch", "val/acc"]` - these fields are included as columns.
                    Namespaces: `["params", "val"]` - all the fields inside the namespaces are included as columns.
                If `None` (default), all the columns of the model versions table are included.

        Returns:
            `Table` object containing `ModelVersion` objects that match the specified criteria.

            Use `to_pandas()` to convert it to a pandas DataFrame.

        Examples:
            >>> import neptune

            >>> # Initialize model with the ID "CLS-FOREST"
            ... model = neptune.init_model(with_id="CLS-FOREST")

            >>> # Fetch the metadata of all model versions as a pandas DataFrame
            ... model_versions_df = model.fetch_model_versions_table().to_pandas()

            >>> # Fetch the metadata of all model versions as a pandas DataFrame,
            ... # including only the fields "params/lr" and "val/loss" as columns:
            ... model_versions = model.fetch_model_versions_table(columns=["params/lr", "val/loss"])
            ... model_versions_df = model_versions.to_pandas()

            >>> # Sort model versions by size
            ... model_versions_df = model_versions_df.sort_values(by="sys/size")

            >>> # Sort model versions by creation time
            ... model_versions_df = model_versions_df.sort_values(by="sys/creation_time", ascending=False)

            >>> # Extract the last model version ID
            ... last_model_version_id = model_versions_df["sys/id"].values[0]

        You may also want to check the API referene in the docs:
            https://docs.neptune.ai/api/model/#fetch_model_versions_table
        """
        return MetadataContainer._fetch_entries(
            self,
            child_type=ContainerType.MODEL_VERSION,
            query=NQLQueryAggregate(
                items=[
                    NQLQueryAttribute(
                        name="sys/model_id",
                        value=self._sys_id,
                        operator=NQLAttributeOperator.EQUALS,
                        type=NQLAttributeType.STRING,
                    ),
                    NQLQueryAttribute(
                        name="sys/trashed",
                        type=NQLAttributeType.BOOLEAN,
                        operator=NQLAttributeOperator.EQUALS,
                        value=False,
                    ),
                ],
                aggregator=NQLAggregator.AND,
            ),
            columns=columns,
        )
