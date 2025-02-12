import logging
from collections.abc import Mapping
from functools import partial

import sentry_sdk
from arroyo import Topic, configure_metrics
from arroyo.backends.kafka.configuration import build_kafka_consumer_configuration
from arroyo.backends.kafka.consumer import KafkaConsumer, KafkaPayload
from arroyo.commit import ONCE_PER_SECOND
from arroyo.processing.processor import StreamProcessor
from arroyo.processing.strategies import (
    CommitOffsets,
    ProcessingStrategy,
    ProcessingStrategyFactory,
    RunTask,
)
from arroyo.types import BrokerValue, Commit, Message, Partition
from sentry_kafka_schemas import get_codec

from sentry.features.rollout import in_random_rollout
from sentry.snuba.dataset import Dataset
from sentry.snuba.query_subscriptions.constants import dataset_to_logical_topic, topic_to_dataset
from sentry.utils.arroyo import MultiprocessingPool, RunTaskWithMultiprocessing

logger = logging.getLogger(__name__)


class QuerySubscriptionStrategyFactory(ProcessingStrategyFactory[KafkaPayload]):
    def __init__(
        self,
        topic: str,
        max_batch_size: int,
        max_batch_time: int,
        num_processes: int,
        input_block_size: int | None,
        output_block_size: int | None,
        multi_proc: bool = True,
    ):
        self.topic = topic
        self.dataset = topic_to_dataset[self.topic]
        self.logical_topic = dataset_to_logical_topic[self.dataset]
        self.max_batch_size = max_batch_size
        self.max_batch_time = max_batch_time
        self.input_block_size = input_block_size
        self.output_block_size = output_block_size
        self.multi_proc = multi_proc
        self.pool = MultiprocessingPool(num_processes)

    def create_with_partitions(
        self,
        commit: Commit,
        partitions: Mapping[Partition, int],
    ) -> ProcessingStrategy[KafkaPayload]:
        callable = partial(process_message, self.dataset, self.topic, self.logical_topic)
        if self.multi_proc:
            return RunTaskWithMultiprocessing(
                function=callable,
                next_step=CommitOffsets(commit),
                max_batch_size=self.max_batch_size,
                max_batch_time=self.max_batch_time,
                pool=self.pool,
                input_block_size=self.input_block_size,
                output_block_size=self.output_block_size,
            )
        else:
            return RunTask(callable, CommitOffsets(commit))

    def shutdown(self) -> None:
        self.pool.close()


def process_message(
    dataset: Dataset, topic: str, logical_topic: str, message: Message[KafkaPayload]
) -> None:
    from sentry.snuba.query_subscriptions.consumer import handle_message
    from sentry.utils import metrics

    with sentry_sdk.start_transaction(
        op="handle_message",
        name="query_subscription_consumer_process_message",
        sampled=in_random_rollout("subscriptions-query.sample-rate"),
    ), metrics.timer("snuba_query_subscriber.handle_message", tags={"dataset": dataset.value}):
        value = message.value
        assert isinstance(value, BrokerValue)
        offset = value.offset
        partition = value.partition.index
        message_value = value.payload.value
        try:
            handle_message(
                message_value,
                offset,
                partition,
                topic,
                dataset.value,
                get_codec(logical_topic),
            )
        except Exception:
            # This is a failsafe to make sure that no individual message will block this
            # consumer. If we see errors occurring here they need to be investigated to
            # make sure that we're not dropping legitimate messages.
            logger.exception(
                "Unexpected error while handling message in QuerySubscriptionStrategy. Skipping message.",
                extra={
                    "offset": offset,
                    "partition": partition,
                    "value": message_value,
                },
            )


def get_query_subscription_consumer(
    topic: str,
    group_id: str,
    strict_offset_reset: bool,
    initial_offset_reset: str,
    max_batch_size: int,
    max_batch_time: int,
    num_processes: int,
    input_block_size: int | None,
    output_block_size: int | None,
    multi_proc: bool = False,
) -> StreamProcessor[KafkaPayload]:
    from sentry.utils import kafka_config

    cluster_name = kafka_config.get_topic_definition(topic)["cluster"]
    cluster_options = kafka_config.get_kafka_consumer_cluster_options(cluster_name)

    initialize_metrics(group_id=group_id)

    consumer = KafkaConsumer(
        build_kafka_consumer_configuration(
            cluster_options,
            group_id=group_id,
            strict_offset_reset=strict_offset_reset,
            auto_offset_reset=initial_offset_reset,
        )
    )
    return StreamProcessor(
        consumer=consumer,
        topic=Topic(topic),
        processor_factory=QuerySubscriptionStrategyFactory(
            topic,
            max_batch_size,
            max_batch_time,
            num_processes,
            input_block_size,
            output_block_size,
            multi_proc=multi_proc,
        ),
        commit_policy=ONCE_PER_SECOND,
    )


def initialize_metrics(group_id: str) -> None:
    from sentry.utils import metrics
    from sentry.utils.arroyo import MetricsWrapper

    metrics_wrapper = MetricsWrapper(
        metrics.backend, name="query_subscription_consumer", tags={"consumer_group": group_id}
    )
    configure_metrics(metrics_wrapper)
