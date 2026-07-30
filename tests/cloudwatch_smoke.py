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
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "my-aurora-cluster"}],
                "Timestamp": metric_time,
                "Value": 91.0,
                "Unit": "Percent",
            },
            {
                "MetricName": "DatabaseConnections",
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "my-aurora-cluster"}],
                "Timestamp": metric_time,
                "Value": 104.0,
                "Unit": "Count",
            },
        ],
    )

    store = Store(DB_PATH)
    collector = CloudWatchCollector("my-aurora-cluster", store, region="us-east-1")
    results = collector.poll_once()
    print(f"poll_once() returned: {results}")

    assert results.get("CPUUtilization") == 91.0, f"expected CPUUtilization=91.0, got {results}"
    assert results.get("DatabaseConnections") == 104.0, f"expected DatabaseConnections=104.0, got {results}"

    rows = store.cloudwatch_metrics_in(0, time.time() + 1)
    metrics_stored = {m: v for _, m, v in rows}
    assert metrics_stored.get("CPUUtilization") == 91.0
    assert metrics_stored.get("DatabaseConnections") == 104.0
    store.close()

    print("\nPASS: CloudWatch collector correctly fetches and stores real metric data (mocked via moto)")


run()
