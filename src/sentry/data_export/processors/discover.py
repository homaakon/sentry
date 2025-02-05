import logging

from sentry_relay.consts import SPAN_STATUS_CODE_TO_NAME

from sentry.api.utils import get_date_range_from_params
from sentry.models.environment import Environment
from sentry.models.group import Group
from sentry.models.project import Project
from sentry.search.events.fields import get_function_alias
from sentry.snuba import discover
from sentry.snuba.utils import get_dataset

from ..base import ExportError

logger = logging.getLogger(__name__)


class DiscoverProcessor:
    """
    Processor for exports of discover data based on a provided query
    """

    def __init__(self, organization_id, discover_query):
        self.projects = self.get_projects(organization_id, discover_query)
        self.environments = self.get_environments(organization_id, discover_query)
        self.start, self.end = get_date_range_from_params(discover_query)
        self.params = {
            "organization_id": organization_id,
            "project_id": [project.id for project in self.projects],
            "start": self.start,
            "end": self.end,
        }
        # make sure to only include environment if any are given
        # an empty list DOES NOT work
        if self.environments:
            self.params["environment"] = self.environments

        equations = discover_query.get("equations", [])
        self.header_fields = [get_function_alias(x) for x in discover_query["field"]] + equations
        self.equation_aliases = {
            f"equation[{index}]": equation for index, equation in enumerate(equations)
        }
        self.data_fn = self.get_data_fn(
            fields=discover_query["field"],
            equations=equations,
            query=discover_query["query"],
            params=self.params,
            sort=discover_query.get("sort"),
            dataset=discover_query.get("dataset"),
        )

    @staticmethod
    def get_projects(organization_id, query):
        projects = list(Project.objects.filter(id__in=query.get("project")))
        if len(projects) == 0:
            raise ExportError("Requested project does not exist")
        return projects

    @staticmethod
    def get_environments(organization_id, query):
        requested_environments = query.get("environment", [])
        if not isinstance(requested_environments, list):
            requested_environments = [requested_environments]

        if not requested_environments:
            return []

        environments = list(
            Environment.objects.filter(
                organization_id=organization_id, name__in=requested_environments
            )
        )
        environment_names = [e.name for e in environments]

        if set(requested_environments) != set(environment_names):
            raise ExportError("Requested environment does not exist")

        return environment_names

    @staticmethod
    def get_data_fn(fields, equations, query, params, sort, dataset):
        dataset = get_dataset(dataset)
        if dataset is None:
            dataset = discover

        def data_fn(offset, limit):
            return dataset.query(
                selected_columns=fields,
                equations=equations,
                query=query,
                params=params,
                offset=offset,
                orderby=sort,
                limit=limit,
                referrer="data_export.tasks.discover",
                auto_fields=True,
                auto_aggregations=True,
                use_aggregate_conditions=True,
            )

        return data_fn

    def handle_fields(self, result_list):
        # Find issue short_id if present
        # (originally in `/api/bases/organization_events.py`)
        new_result_list = result_list[:]

        if "issue" in self.header_fields:
            issue_ids = {result["issue.id"] for result in new_result_list}
            issues = {
                i.id: i.qualified_short_id
                for i in Group.objects.filter(
                    id__in=issue_ids,
                    project__in=self.params["project_id"],
                    project__organization_id=self.params["organization_id"],
                )
            }
            for result in new_result_list:
                if "issue.id" in result:
                    result["issue"] = issues.get(result["issue.id"], "unknown")

        if "transaction.status" in self.header_fields:
            for result in new_result_list:
                if "transaction.status" in result:
                    result["transaction.status"] = SPAN_STATUS_CODE_TO_NAME.get(
                        result["transaction.status"], "unknown"
                    )

        # Map equations back to their unaliased forms
        if self.equation_aliases:
            for result in new_result_list:
                for equation_alias, equation in self.equation_aliases.items():
                    result[equation] = result.get(equation_alias)

        return new_result_list
