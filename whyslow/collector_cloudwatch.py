import time
from datetime import datetime, timezone, timedelta

from .storage import Store

METRICS = ["CPUUtilization", "DatabaseConnections"]

# CloudWatch metric publication has inherent lag -- querying with
# EndTime=now risks the most recent period not being queryable yet and
# silently returning nothing for it. Standard practice (and confirmed by
# testing against a mock CloudWatch): pull the query window back by a
# buffer instead of querying right up to the current instant.
QUERY_END_BUFFER_SECONDS = 120


class CloudWatchCollector:
    """Polls basic RDS/Aurora CloudWatch metrics. Requires `boto3` and AWS
    credentials with cloudwatch:GetMetricData on the instance. Verified
    against a mocked CloudWatch (moto) in tests/cloudwatch_smoke.py --
    not yet run against a real AWS account."""

    def __init__(self, db_instance_id, store: Store, interval=60, region=None):
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "CloudWatch collector requires boto3: pip install 'whyslow[cloudwatch]'"
            ) from e
        self.boto3 = boto3
        self.client = boto3.client("cloudwatch", region_name=region)
        self.db_instance_id = db_instance_id
        self.store = store
        self.interval = interval

    def poll_once(self):
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=QUERY_END_BUFFER_SECONDS)
        start = end - timedelta(seconds=max(self.interval * 3, QUERY_END_BUFFER_SECONDS))
        results = {}
        for metric in METRICS:
            resp = self.client.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=metric,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": self.db_instance_id}],
                StartTime=start,
                EndTime=end,
                Period=60,
                Statistics=["Average"],
            )
            points = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
            if points:
                point = points[-1]
                value = point["Average"]
                # The query intentionally looks behind "now" to tolerate
                # CloudWatch publication lag. Recording collection time here
                # shifted evidence by minutes and broke 10-second incident
                # correlation. Preserve the measurement's real timestamp.
                self.store.write_cloudwatch_metric(
                    metric,
                    value,
                    ts=point["Timestamp"].timestamp(),
                )
                results[metric] = value
        return results

    def run_forever(self):
        print(f"[whyslow] cloudwatch collector polling {self.db_instance_id} every {self.interval}s")
        consecutive_failures = 0
        while True:
            try:
                results = self.poll_once()
                self.store.write_heartbeat(
                    "cloudwatch",
                    detail=f"metrics={sorted(results.keys())}" if results else "no datapoints",
                )
                consecutive_failures = 0
                print(f"[whyslow] {results}")
                time.sleep(self.interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                # Transient AWS API errors / throttling must not kill the
                # collector -- they previously did.
                consecutive_failures += 1
                if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                    print(f"[whyslow] cloudwatch poll failed "
                          f"({consecutive_failures}x): {e}")
                time.sleep(min(self.interval * consecutive_failures, 300))
