import asyncio
import logging
from collections import defaultdict
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from autogpt_libs.utils.cache import thread_cached
from pydantic import BaseModel

from backend.data.block import (
    Block,
    BlockData,
    BlockInput,
    BlockSchema,
    BlockType,
    get_block,
)
from backend.data.block_cost_config import BLOCK_COSTS
from backend.data.cost import BlockCostType
from backend.data.execution import (
    AsyncRedisExecutionEventBus,
    ExecutionStatus,
    GraphExecutionStats,
    GraphExecutionWithNodes,
    RedisExecutionEventBus,
    create_graph_execution,
    get_node_executions,
    update_graph_execution_stats,
    update_node_execution_status_batch,
)
from backend.data.graph import GraphModel, Node, get_graph
from backend.data.model import CredentialsMetaInput
from backend.data.rabbitmq import (
    AsyncRabbitMQ,
    Exchange,
    ExchangeType,
    Queue,
    RabbitMQConfig,
    SyncRabbitMQ,
)
from backend.util.exceptions import NotFoundError
from backend.util.logging import TruncatedLogger
from backend.util.mock import MockObject
from backend.util.service import get_service_client
from backend.util.settings import Config
from backend.util.type import convert

if TYPE_CHECKING:
    from backend.executor import DatabaseManagerAsyncClient, DatabaseManagerClient
    from backend.integrations.credentials_store import IntegrationCredentialsStore

config = Config()
logger = TruncatedLogger(logging.getLogger(__name__), prefix="[GraphExecutorUtil]")

# ============ Resource Helpers ============ #


@thread_cached
def get_execution_event_bus() -> RedisExecutionEventBus:
    return RedisExecutionEventBus()


@thread_cached
def get_async_execution_event_bus() -> AsyncRedisExecutionEventBus:
    return AsyncRedisExecutionEventBus()


@thread_cached
def get_execution_queue() -> SyncRabbitMQ:
    client = SyncRabbitMQ(create_execution_queue_config())
    client.connect()
    return client


@thread_cached
async def get_async_execution_queue() -> AsyncRabbitMQ:
    client = AsyncRabbitMQ(create_execution_queue_config())
    await client.connect()
    return client


@thread_cached
def get_integration_credentials_store() -> "IntegrationCredentialsStore":
    from backend.integrations.credentials_store import IntegrationCredentialsStore

    return IntegrationCredentialsStore()


@thread_cached
def get_db_client() -> "DatabaseManagerClient":
    from backend.executor import DatabaseManagerClient

    return get_service_client(DatabaseManagerClient)


@thread_cached
def get_db_async_client() -> "DatabaseManagerAsyncClient":
    from backend.executor import DatabaseManagerAsyncClient

    return get_service_client(DatabaseManagerAsyncClient)


# ============ Execution Cost Helpers ============ #


def execution_usage_cost(execution_count: int) -> tuple[int, int]:
    """
    Calculate the cost of executing a graph based on the current number of node executions.

    Args:
        execution_count: Number of node executions

    Returns:
        Tuple of cost amount and the number of execution count that is included in the cost.
    """
    return (
        (
            config.execution_cost_per_threshold
            if execution_count % config.execution_cost_count_threshold == 0
            else 0
        ),
        config.execution_cost_count_threshold,
    )


def block_usage_cost(
    block: Block,
    input_data: BlockInput,
    data_size: float = 0,
    run_time: float = 0,
) -> tuple[int, BlockInput]:
    """
    Calculate the cost of using a block based on the input data and the block type.

    Args:
        block: Block object
        input_data: Input data for the block
        data_size: Size of the input data in bytes
        run_time: Execution time of the block in seconds

    Returns:
        Tuple of cost amount and cost filter
    """
    block_costs = BLOCK_COSTS.get(type(block))
    if not block_costs:
        return 0, {}

    for block_cost in block_costs:
        if not _is_cost_filter_match(block_cost.cost_filter, input_data):
            continue

        if block_cost.cost_type == BlockCostType.RUN:
            return block_cost.cost_amount, block_cost.cost_filter

        if block_cost.cost_type == BlockCostType.SECOND:
            return (
                int(run_time * block_cost.cost_amount),
                block_cost.cost_filter,
            )

        if block_cost.cost_type == BlockCostType.BYTE:
            return (
                int(data_size * block_cost.cost_amount),
                block_cost.cost_filter,
            )

    return 0, {}


def _is_cost_filter_match(cost_filter: BlockInput, input_data: BlockInput) -> bool:
    """
    Filter rules:
      - If cost_filter is an object, then check if cost_filter is the subset of input_data
      - Otherwise, check if cost_filter is equal to input_data.
      - Undefined, null, and empty string are considered as equal.
    """
    if not isinstance(cost_filter, dict) or not isinstance(input_data, dict):
        return cost_filter == input_data

    return all(
        (not input_data.get(k) and not v)
        or (input_data.get(k) and _is_cost_filter_match(v, input_data[k]))
        for k, v in cost_filter.items()
    )


# ============ Execution Input Helpers ============ #

# --------------------------------------------------------------------------- #
#  Delimiters
# --------------------------------------------------------------------------- #

LIST_SPLIT = "_$_"
DICT_SPLIT = "_#_"
OBJC_SPLIT = "_@_"

_DELIMS = (LIST_SPLIT, DICT_SPLIT, OBJC_SPLIT)

# --------------------------------------------------------------------------- #
#  Tokenisation utilities
# --------------------------------------------------------------------------- #


def _next_delim(s: str) -> tuple[str | None, int]:
    """
    Return the *earliest* delimiter appearing in `s` and its index.

    If none present → (None, -1).
    """
    first: str | None = None
    pos = len(s)  # sentinel: larger than any real index
    for d in _DELIMS:
        i = s.find(d)
        if 0 <= i < pos:
            first, pos = d, i
    return first, (pos if first else -1)


def _tokenise(path: str) -> list[tuple[str, str]] | None:
    """
    Convert the raw path string (starting with a delimiter) into
    [ (delimiter, identifier), … ] or None if the syntax is malformed.
    """
    tokens: list[tuple[str, str]] = []
    while path:
        # 1. Which delimiter starts this chunk?
        delim = next((d for d in _DELIMS if path.startswith(d)), None)
        if delim is None:
            return None  # invalid syntax

        # 2. Slice off the delimiter, then up to the next delimiter (or EOS)
        path = path[len(delim) :]
        nxt_delim, pos = _next_delim(path)
        token, path = (
            path[: pos if pos != -1 else len(path)],
            path[pos if pos != -1 else len(path) :],
        )
        if token == "":
            return None  # empty identifier is invalid
        tokens.append((delim, token))
    return tokens


# --------------------------------------------------------------------------- #
#  Public API – parsing (flattened ➜ concrete)
# --------------------------------------------------------------------------- #


def parse_execution_output(output: BlockData, name: str) -> Any | None:
    """
    Retrieve a nested value out of `output` using the flattened *name*.

    On any failure (wrong name, wrong type, out-of-range, bad path)
    returns **None**.
    """
    base_name, data = output

    # Exact match → whole object
    if name == base_name:
        return data

    # Must start with the expected name
    if not name.startswith(base_name):
        return None
    path = name[len(base_name) :]
    if not path:
        return None  # nothing left to parse

    tokens = _tokenise(path)
    if tokens is None:
        return None

    cur: Any = data
    for delim, ident in tokens:
        if delim == LIST_SPLIT:
            # list[index]
            try:
                idx = int(ident)
            except ValueError:
                return None
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]

        elif delim == DICT_SPLIT:
            if not isinstance(cur, dict) or ident not in cur:
                return None
            cur = cur[ident]

        elif delim == OBJC_SPLIT:
            if not hasattr(cur, ident):
                return None
            cur = getattr(cur, ident)

        else:
            return None  # unreachable

    return cur


def _assign(container: Any, tokens: list[tuple[str, str]], value: Any) -> Any:
    """
    Recursive helper that *returns* the (possibly new) container with
    `value` assigned along the remaining `tokens` path.
    """
    if not tokens:
        return value  # leaf reached

    delim, ident = tokens[0]
    rest = tokens[1:]

    # ---------- list ----------
    if delim == LIST_SPLIT:
        try:
            idx = int(ident)
        except ValueError:
            raise ValueError("index must be an integer")

        if container is None:
            container = []
        elif not isinstance(container, list):
            container = list(container) if hasattr(container, "__iter__") else []

        while len(container) <= idx:
            container.append(None)
        container[idx] = _assign(container[idx], rest, value)
        return container

    # ---------- dict ----------
    if delim == DICT_SPLIT:
        if container is None:
            container = {}
        elif not isinstance(container, dict):
            container = dict(container) if hasattr(container, "items") else {}
        container[ident] = _assign(container.get(ident), rest, value)
        return container

    # ---------- object ----------
    if delim == OBJC_SPLIT:
        if container is None or not isinstance(container, MockObject):
            container = MockObject()
        setattr(
            container,
            ident,
            _assign(getattr(container, ident, None), rest, value),
        )
        return container

    return value  # unreachable


def merge_execution_input(data: BlockInput) -> BlockInput:
    """
    Reconstruct nested objects from a *flattened* dict of key → value.

    Raises ValueError on syntactically invalid list indices.
    """
    merged: BlockInput = {}

    for key, value in data.items():
        # Split off the base name (before the first delimiter, if any)
        delim, pos = _next_delim(key)
        if delim is None:
            merged[key] = value
            continue

        base, path = key[:pos], key[pos:]
        tokens = _tokenise(path)
        if tokens is None:
            # Invalid key; treat as scalar under the raw name
            merged[key] = value
            continue

        merged[base] = _assign(merged.get(base), tokens, value)

    data.update(merged)
    return data


def validate_exec(
    node: Node,
    data: BlockInput,
    resolve_input: bool = True,
) -> tuple[BlockInput | None, str]:
    """
    Validate the input data for a node execution.

    Args:
        node: The node to execute.
        data: The input data for the node execution.
        resolve_input: Whether to resolve dynamic pins into dict/list/object.

    Returns:
        A tuple of the validated data and the block name.
        If the data is invalid, the first element will be None, and the second element
        will be an error message.
        If the data is valid, the first element will be the resolved input data, and
        the second element will be the block name.
    """
    node_block = get_block(node.block_id)
    if not node_block:
        return None, f"Block for {node.block_id} not found."
    schema = node_block.input_schema

    # Convert non-matching data types to the expected input schema.
    for name, data_type in schema.__annotations__.items():
        value = data.get(name)
        if (value is not None) and (type(value) is not data_type):
            data[name] = convert(value, data_type)

    # Input data (without default values) should contain all required fields.
    error_prefix = f"Input data missing or mismatch for `{node_block.name}`:"
    if missing_links := schema.get_missing_links(data, node.input_links):
        return None, f"{error_prefix} unpopulated links {missing_links}"

    # Merge input data with default values and resolve dynamic dict/list/object pins.
    input_default = schema.get_input_defaults(node.input_default)
    data = {**input_default, **data}
    if resolve_input:
        data = merge_execution_input(data)

    # Input data post-merge should contain all required fields from the schema.
    if missing_input := schema.get_missing_input(data):
        return None, f"{error_prefix} missing input {missing_input}"

    # Last validation: Validate the input values against the schema.
    if error := schema.get_mismatch_error(data):
        error_message = f"{error_prefix} {error}"
        logger.error(error_message)
        return None, error_message

    return data, node_block.name


async def _validate_node_input_credentials(
    graph: GraphModel,
    user_id: str,
    node_credentials_input_map: Optional[
        dict[str, dict[str, CredentialsMetaInput]]
    ] = None,
):
    """Checks all credentials for all nodes of the graph"""

    for node in graph.nodes:
        block = node.block

        # Find any fields of type CredentialsMetaInput
        credentials_fields = cast(
            type[BlockSchema], block.input_schema
        ).get_credentials_fields()
        if not credentials_fields:
            continue

        for field_name, credentials_meta_type in credentials_fields.items():
            if (
                node_credentials_input_map
                and (node_credentials_inputs := node_credentials_input_map.get(node.id))
                and field_name in node_credentials_inputs
            ):
                credentials_meta = node_credentials_input_map[node.id][field_name]
            elif field_name in node.input_default:
                credentials_meta = credentials_meta_type.model_validate(
                    node.input_default[field_name]
                )
            else:
                raise ValueError(
                    f"Credentials absent for {block.name} node #{node.id} "
                    f"input '{field_name}'"
                )

            # Fetch the corresponding Credentials and perform sanity checks
            credentials = await get_integration_credentials_store().get_creds_by_id(
                user_id, credentials_meta.id
            )
            if not credentials:
                raise ValueError(
                    f"Unknown credentials #{credentials_meta.id} "
                    f"for node #{node.id} input '{field_name}'"
                )
            if (
                credentials.provider != credentials_meta.provider
                or credentials.type != credentials_meta.type
            ):
                logger.warning(
                    f"Invalid credentials #{credentials.id} for node #{node.id}: "
                    "type/provider mismatch: "
                    f"{credentials_meta.type}<>{credentials.type};"
                    f"{credentials_meta.provider}<>{credentials.provider}"
                )
                raise ValueError(
                    f"Invalid credentials #{credentials.id} for node #{node.id}: "
                    "type/provider mismatch"
                )


def make_node_credentials_input_map(
    graph: GraphModel,
    graph_credentials_input: dict[str, CredentialsMetaInput],
) -> dict[str, dict[str, CredentialsMetaInput]]:
    """
    Maps credentials for an execution to the correct nodes.

    Params:
        graph: The graph to be executed.
        graph_credentials_input: A (graph_input_name, credentials_meta) map.

    Returns:
        dict[node_id, dict[field_name, CredentialsMetaInput]]: Node credentials input map.
    """
    result: dict[str, dict[str, CredentialsMetaInput]] = {}

    # Get aggregated credentials fields for the graph
    graph_cred_inputs = graph.aggregate_credentials_inputs()

    for graph_input_name, (_, compatible_node_fields) in graph_cred_inputs.items():
        # Best-effort map: skip missing items
        if graph_input_name not in graph_credentials_input:
            continue

        # Use passed-in credentials for all compatible node input fields
        for node_id, node_field_name in compatible_node_fields:
            if node_id not in result:
                result[node_id] = {}
            result[node_id][node_field_name] = graph_credentials_input[graph_input_name]

    return result


async def construct_node_execution_input(
    graph: GraphModel,
    user_id: str,
    graph_inputs: BlockInput,
    node_credentials_input_map: Optional[
        dict[str, dict[str, CredentialsMetaInput]]
    ] = None,
) -> list[tuple[str, BlockInput]]:
    """
    Validates and prepares the input data for executing a graph.
    This function checks the graph for starting nodes, validates the input data
    against the schema, and resolves dynamic input pins into a single list,
    dictionary, or object.

    Args:
        graph (GraphModel): The graph model to execute.
        user_id (str): The ID of the user executing the graph.
        data (BlockInput): The input data for the graph execution.
        node_credentials_map: `dict[node_id, dict[input_name, CredentialsMetaInput]]`

    Returns:
        list[tuple[str, BlockInput]]: A list of tuples, each containing the node ID and
            the corresponding input data for that node.
    """
    graph.validate_graph(for_run=True)
    await _validate_node_input_credentials(graph, user_id, node_credentials_input_map)

    nodes_input = []
    for node in graph.starting_nodes:
        input_data = {}
        block = node.block

        # Note block should never be executed.
        if block.block_type == BlockType.NOTE:
            continue

        # Extract request input data, and assign it to the input pin.
        if block.block_type == BlockType.INPUT:
            input_name = node.input_default.get("name")
            if input_name and input_name in graph_inputs:
                input_data = {"value": graph_inputs[input_name]}

        # Extract webhook payload, and assign it to the input pin
        webhook_payload_key = f"webhook_{node.webhook_id}_payload"
        if (
            block.block_type in (BlockType.WEBHOOK, BlockType.WEBHOOK_MANUAL)
            and node.webhook_id
        ):
            if webhook_payload_key not in graph_inputs:
                raise ValueError(
                    f"Node {block.name} #{node.id} webhook payload is missing"
                )
            input_data = {"payload": graph_inputs[webhook_payload_key]}

        # Apply node credentials overrides
        if node_credentials_input_map and (
            node_credentials := node_credentials_input_map.get(node.id)
        ):
            input_data.update({k: v.model_dump() for k, v in node_credentials.items()})

        input_data, error = validate_exec(node, input_data)
        if input_data is None:
            raise ValueError(error)
        else:
            nodes_input.append((node.id, input_data))

    if not nodes_input:
        raise ValueError(
            "No starting nodes found for the graph, make sure an AgentInput or blocks with no inbound links are present as starting nodes."
        )

    return nodes_input


# ============ Execution Queue Helpers ============ #


class CancelExecutionEvent(BaseModel):
    graph_exec_id: str


GRAPH_EXECUTION_EXCHANGE = Exchange(
    name="graph_execution",
    type=ExchangeType.DIRECT,
    durable=True,
    auto_delete=False,
)
GRAPH_EXECUTION_QUEUE_NAME = "graph_execution_queue"
GRAPH_EXECUTION_ROUTING_KEY = "graph_execution.run"

GRAPH_EXECUTION_CANCEL_EXCHANGE = Exchange(
    name="graph_execution_cancel",
    type=ExchangeType.FANOUT,
    durable=True,
    auto_delete=True,
)
GRAPH_EXECUTION_CANCEL_QUEUE_NAME = "graph_execution_cancel_queue"


def create_execution_queue_config() -> RabbitMQConfig:
    """
    Define two exchanges and queues:
    - 'graph_execution' (DIRECT) for run tasks.
    - 'graph_execution_cancel' (FANOUT) for cancel requests.
    """
    run_queue = Queue(
        name=GRAPH_EXECUTION_QUEUE_NAME,
        exchange=GRAPH_EXECUTION_EXCHANGE,
        routing_key=GRAPH_EXECUTION_ROUTING_KEY,
        durable=True,
        auto_delete=False,
    )
    cancel_queue = Queue(
        name=GRAPH_EXECUTION_CANCEL_QUEUE_NAME,
        exchange=GRAPH_EXECUTION_CANCEL_EXCHANGE,
        routing_key="",  # not used for FANOUT
        durable=True,
        auto_delete=False,
    )
    return RabbitMQConfig(
        vhost="/",
        exchanges=[GRAPH_EXECUTION_EXCHANGE, GRAPH_EXECUTION_CANCEL_EXCHANGE],
        queues=[run_queue, cancel_queue],
    )


async def stop_graph_execution(
    graph_exec_id: str,
    use_db_query: bool = True,
):
    """
    Mechanism:
    1. Set the cancel event
    2. Graph executor's cancel handler thread detects the event, terminates workers,
       reinitializes worker pool, and returns.
    3. Update execution statuses in DB and set `error` outputs to `"TERMINATED"`.
    """
    queue_client = await get_async_execution_queue()
    await queue_client.publish_message(
        routing_key="",
        message=CancelExecutionEvent(graph_exec_id=graph_exec_id).model_dump_json(),
        exchange=GRAPH_EXECUTION_CANCEL_EXCHANGE,
    )

    # Update the status of the graph execution
    if use_db_query:
        graph_execution = await update_graph_execution_stats(
            graph_exec_id,
            ExecutionStatus.TERMINATED,
        )
    else:
        graph_execution = await get_db_async_client().update_graph_execution_stats(
            graph_exec_id,
            ExecutionStatus.TERMINATED,
        )

    if graph_execution:
        await get_async_execution_event_bus().publish(graph_execution)
    else:
        raise NotFoundError(
            f"Graph execution #{graph_exec_id} not found for termination."
        )

    # Update the status of the node executions
    if use_db_query:
        node_executions = await get_node_executions(
            graph_exec_id=graph_exec_id,
            statuses=[
                ExecutionStatus.QUEUED,
                ExecutionStatus.RUNNING,
                ExecutionStatus.INCOMPLETE,
            ],
        )
        await update_node_execution_status_batch(
            [v.node_exec_id for v in node_executions],
            ExecutionStatus.TERMINATED,
        )
    else:
        node_executions = await get_db_async_client().get_node_executions(
            graph_exec_id=graph_exec_id,
            statuses=[
                ExecutionStatus.QUEUED,
                ExecutionStatus.RUNNING,
                ExecutionStatus.INCOMPLETE,
            ],
        )
        await get_db_async_client().update_node_execution_status_batch(
            [v.node_exec_id for v in node_executions],
            ExecutionStatus.TERMINATED,
        )

    await asyncio.gather(
        *[
            get_async_execution_event_bus().publish(
                v.model_copy(update={"status": ExecutionStatus.TERMINATED})
            )
            for v in node_executions
        ]
    )


async def add_graph_execution(
    graph_id: str,
    user_id: str,
    inputs: BlockInput,
    preset_id: Optional[str] = None,
    graph_version: Optional[int] = None,
    graph_credentials_inputs: Optional[dict[str, CredentialsMetaInput]] = None,
    node_credentials_input_map: Optional[
        dict[str, dict[str, CredentialsMetaInput]]
    ] = None,
    use_db_query: bool = True,
) -> GraphExecutionWithNodes:
    """
    Adds a graph execution to the queue and returns the execution entry.

    Args:
        graph_id: The ID of the graph to execute.
        user_id: The ID of the user executing the graph.
        inputs: The input data for the graph execution.
        preset_id: The ID of the preset to use.
        graph_version: The version of the graph to execute.
        graph_credentials_inputs: Credentials inputs to use in the execution.
            Keys should map to the keys generated by `GraphModel.aggregate_credentials_inputs`.
        node_credentials_input_map: Credentials inputs to use in the execution, mapped to specific nodes.
    Returns:
        GraphExecutionEntry: The entry for the graph execution.
    Raises:
        ValueError: If the graph is not found or if there are validation errors.
    """  # noqa
    if use_db_query:
        graph: GraphModel | None = await get_graph(
            graph_id=graph_id,
            user_id=user_id,
            version=graph_version,
            include_subgraphs=True,
        )
    else:
        graph: GraphModel | None = await get_db_async_client().get_graph(
            graph_id=graph_id,
            user_id=user_id,
            version=graph_version,
            include_subgraphs=True,
        )

    if not graph:
        raise NotFoundError(f"Graph #{graph_id} not found.")

    node_credentials_input_map = node_credentials_input_map or (
        make_node_credentials_input_map(graph, graph_credentials_inputs)
        if graph_credentials_inputs
        else None
    )

    if use_db_query:
        graph_exec = await create_graph_execution(
            user_id=user_id,
            graph_id=graph_id,
            graph_version=graph.version,
            starting_nodes_input=await construct_node_execution_input(
                graph=graph,
                user_id=user_id,
                graph_inputs=inputs,
                node_credentials_input_map=node_credentials_input_map,
            ),
            preset_id=preset_id,
        )
    else:
        graph_exec = await get_db_async_client().create_graph_execution(
            user_id=user_id,
            graph_id=graph_id,
            graph_version=graph.version,
            starting_nodes_input=await construct_node_execution_input(
                graph=graph,
                user_id=user_id,
                graph_inputs=inputs,
                node_credentials_input_map=node_credentials_input_map,
            ),
            preset_id=preset_id,
        )

    try:
        queue = await get_async_execution_queue()
        graph_exec_entry = graph_exec.to_graph_execution_entry()
        if node_credentials_input_map:
            graph_exec_entry.node_credentials_input_map = node_credentials_input_map
        await queue.publish_message(
            routing_key=GRAPH_EXECUTION_ROUTING_KEY,
            message=graph_exec_entry.model_dump_json(),
            exchange=GRAPH_EXECUTION_EXCHANGE,
        )

        bus = get_async_execution_event_bus()
        await bus.publish(graph_exec)

        return graph_exec
    except Exception as e:
        logger.error(f"Unable to publish graph #{graph_id} exec #{graph_exec.id}: {e}")

        if use_db_query:
            await update_node_execution_status_batch(
                [node_exec.node_exec_id for node_exec in graph_exec.node_executions],
                ExecutionStatus.FAILED,
            )
            await update_graph_execution_stats(
                graph_exec_id=graph_exec.id,
                status=ExecutionStatus.FAILED,
                stats=GraphExecutionStats(error=str(e)),
            )
        else:
            await get_db_async_client().update_node_execution_status_batch(
                [node_exec.node_exec_id for node_exec in graph_exec.node_executions],
                ExecutionStatus.FAILED,
            )
            await get_db_async_client().update_graph_execution_stats(
                graph_exec_id=graph_exec.id,
                status=ExecutionStatus.FAILED,
                stats=GraphExecutionStats(error=str(e)),
            )

        raise


# ============ Execution Output Helpers ============ #


class ExecutionOutputEntry(BaseModel):
    node: Node
    node_exec_id: str
    data: BlockData


class NodeExecutionProgress:
    def __init__(
        self,
        on_done_task: Callable[[str, object], None],
    ):
        self.output: dict[str, list[ExecutionOutputEntry]] = defaultdict(list)
        self.tasks: dict[str, Future] = {}
        self.on_done_task = on_done_task

    def add_task(self, node_exec_id: str, task: Future):
        self.tasks[node_exec_id] = task

    def add_output(self, output: ExecutionOutputEntry):
        self.output[output.node_exec_id].append(output)

    def pop_output(self) -> ExecutionOutputEntry | None:
        exec_id = self._next_exec()
        if not exec_id:
            return None

        if self._pop_done_task(exec_id):
            return self.pop_output()

        if next_output := self.output[exec_id]:
            return next_output.pop(0)

        return None

    def is_done(self, wait_time: float = 0.0) -> bool:
        exec_id = self._next_exec()
        if not exec_id:
            return True

        if self._pop_done_task(exec_id):
            return self.is_done(wait_time)

        if wait_time <= 0:
            return False

        try:
            self.tasks[exec_id].result(wait_time)
        except TimeoutError:
            print(
                ">>>>>>>  -- Timeout, after waiting for",
                wait_time,
                "seconds for node_id",
                exec_id,
            )
            pass

        return self.is_done(0)

    def stop(self) -> list[str]:
        """
        Stops all tasks and clears the output.
        This is useful for cleaning up when the execution is cancelled or terminated.
        Returns a list of execution IDs that were stopped.
        """
        cancelled_ids = []
        for task_id, task in self.tasks.items():
            if task.done():
                continue
            task.cancel()
            cancelled_ids.append(task_id)
        return cancelled_ids

    def _pop_done_task(self, exec_id: str) -> bool:
        task = self.tasks.get(exec_id)
        if not task:
            return True

        if not task.done():
            return False

        if self.output[exec_id]:
            return False

        if task := self.tasks.pop(exec_id):
            self.on_done_task(exec_id, task.result())

        return True

    def _next_exec(self) -> str | None:
        if not self.tasks:
            return None
        return next(iter(self.tasks.keys()))
