import os
import shutil
import time
from datetime import datetime, timezone, timedelta

# moto requires *some* credentials present, even fake ones, before it
# intercepts the calls -- it never touches real AWS.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from moto import mock_aws
import boto3

from whyslow.storage import Store

DB_PATH = "/tmp/cw_smoke/store.sqlite3"
shutil.rmtree("/tmp/cw_smoke", ignore_errors=True)


class FakeRDS:
    def __init__(self, writer):
        self.writer = writer

    def describe_db_clusters(self, DBClusterIdentifier):
        assert DBClusterIdentifier == "my-aurora-cluster"
        return {"DBClusters": [{"DBClusterMembers": [
            {"DBInstanceIdentifier": self.writer, "IsClusterWriter": True},
            {"DBInstanceIdentifier": "reader-a", "IsClusterWriter": False},
        ]}]}


@mock_aws
def run():
    from whyslow.collector_cloudwatch import CloudWatchCollector

    client = boto3.client("cloudwatch", region_name="us-east-1")
    # Realistic condition: the metric was published a few minutes ago,
    # not at the exact instant poll_once() computes its own "now". This
    # is what actually happens with real CloudWatch publication latency.
    metric_time = datetime.now(timezone.utc) - timedelta(seconds=150)

    client.put_metric_data(
        Namespace="AWS/RDS",
        MetricData=[
            {
                "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "writer-a"}],
                "Timestamp": metric_time,
                "Value": 91.0,
                "Unit": "Percent",
            },
            {
                "MetricName": "DatabaseConnections",
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "writer-a"}],
                "Timestamp": metric_time,
                "Value": 104.0,
                "Unit": "Count",
            },
        ],
    )

    store = Store(DB_PATH)
    rds = FakeRDS("writer-a")
    collector = CloudWatchCollector(
        None,
        store,
        region="us-east-1",
        db_cluster_id="my-aurora-cluster",
        cloudwatch_client=client,
        rds_client=rds,
    )
    results = collector.poll_once()
    print(f"poll_once() returned: {results}")

    assert results.get("CPUUtilization") == 91.0, f"expected CPUUtilization=91.0, got {results}"
    assert results.get("DatabaseConnections") == 104.0, f"expected DatabaseConnections=104.0, got {results}"

    # Poll the same overlapping window again. Publication-lag buffering
    # deliberately causes overlap, but one CloudWatch datapoint must still
    # produce only one stored row.
    collector.poll_once()

    rows = store.cloudwatch_metrics_in(0, time.time() + 1)
    metrics_stored = {m: v for _, m, v, _ in rows}
    assert metrics_stored.get("CPUUtilization") == 91.0
    assert metrics_stored.get("DatabaseConnections") == 104.0
    assert len(rows) == 2, f"overlapping polls duplicated CloudWatch rows: {rows}"
    assert {source for _, _, _, source in rows} == {"writer-a"}
    for stored_ts, _, _, _ in rows:
        assert abs(stored_ts - metric_time.timestamp()) < 1, (
            "must store the CloudWatch measurement timestamp, not collection time"
        )
    # Simulate an Aurora failover. The next poll must resolve the new writer
    # instead of continuing to query the instance that was primary at start.
    rds.writer = "writer-b"
    client.put_metric_data(
        Namespace="AWS/RDS",
        MetricData=[{
            "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "writer-b"}],
            "Timestamp": metric_time,
            "Value": 37.0,
            "Unit": "Percent",
        }],
    )
    failed_over = collector.poll_once()
    assert failed_over.get("CPUUtilization") == 37.0, failed_over
    rows = store.cloudwatch_metrics_in(0, time.time() + 1)
    assert ("writer-b", 37.0) in {(source, value) for _, _, value, source in rows}
    assert len(rows) == 3, rows
    assert store.latest_cloudwatch_source() == "writer-b"
    store.close()

    print("\nPASS: CloudWatch metrics follow Aurora failover and retain source provenance")


run()
