import time
from datetime import datetime, timezone, timedelta

from .storage import Store
from .validation import (
    validate_interval,
    validate_rds_cluster_id,
    validate_rds_instance_id,
)

METRICS = ["CPUUtilization", "DatabaseConnections"]

# CloudWatch metric publication has inherent lag -- querying with
# EndTime=now risks the most recent period not being queryable yet and
# silently returning nothing for it. Standard practice (and confirmed by
# testing against a mock CloudWatch): pull the query window back by a
# buffer instead of querying right up to the current instant.
QUERY_END_BUFFER_SECONDS = 120


def resolve_cluster_writer(rds_client, db_cluster_id):
    """Return the current writer instance for an Aurora cluster."""
    response = rds_client.describe_db_clusters(DBClusterIdentifier=db_cluster_id)
    clusters = response.get("DBClusters", [])
    if len(clusters) != 1:
        raise RuntimeError(f"expected one RDS cluster, received {len(clusters)}")
    writers = [
        member.get("DBInstanceIdentifier")
        for member in clusters[0].get("DBClusterMembers", [])
        if member.get("IsClusterWriter")
    ]
    writers = [writer for writer in writers if writer]
    if len(writers) != 1:
        raise RuntimeError(f"expected one Aurora writer, received {len(writers)}")
    return writers[0]


class CloudWatchCollector:
    """Polls basic RDS/Aurora CloudWatch metrics. Requires `boto3` and AWS
    credentials with cloudwatch:GetMetricStatistics and, for cluster mode,
    rds:DescribeDBClusters. Verified
    against a mocked CloudWatch (moto) in tests/cloudwatch_smoke.py --
    not yet run against a real AWS account."""

    def __init__(
        self,
        db_instance_id,
        store: Store,
        interval=60,
        region=None,
        db_cluster_id=None,
        cloudwatch_client=None,
        rds_client=None,
    ):
        if bool(db_instance_id) == bool(db_cluster_id):
            raise ValueError("specify exactly one of db_instance_id or db_cluster_id")
        self.db_instance_id = validate_rds_instance_id(db_instance_id) if db_instance_id else None
        self.db_cluster_id = validate_rds_cluster_id(db_cluster_id) if db_cluster_id else None
        self.interval = validate_interval(interval)
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "CloudWatch collector requires boto3: pip install 'whyslow[cloudwatch]'"
            ) from e
        self.boto3 = boto3
        self.client = cloudwatch_client or boto3.client("cloudwatch", region_name=region)
        self.rds_client = rds_client
        if self.db_cluster_id and self.rds_client is None:
            self.rds_client = boto3.client("rds", region_name=region)
        self.store = store
        self.current_instance = self.db_instance_id

    def resolve_instance(self):
        if self.db_cluster_id:
            self.current_instance = resolve_cluster_writer(self.rds_client, self.db_cluster_id)
        return self.current_instance

    def poll_once(self):
        db_instance_id = self.resolve_instance()
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=QUERY_END_BUFFER_SECONDS)
        start = end - timedelta(seconds=max(self.interval * 3, QUERY_END_BUFFER_SECONDS))
        results = {}
        for metric in METRICS:
            resp = self.client.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=metric,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
                StartTime=start,
                EndTime=end,
                Period=60,
                Statistics=["Average"],
            )
            points = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
            for point in points:
                # Write every datapoint in the look-back window, not only the
                # newest one. The window widens to interval*3 to tolerate
                # CloudWatch publication lag and to back-fill after a poll
                # failure -- keeping only points[-1] silently dropped the
                # earlier minutes, leaving holes. write_cloudwatch_metric is
                # idempotent per (metric, ts, source), so re-writing overlap
                # from consecutive polls is safe.
                #
                # The query intentionally looks behind "now" to tolerate
                # publication lag. Recording collection time here shifted
                # evidence by minutes and broke 10-second incident
                # correlation. Preserve each measurement's real timestamp.
                self.store.write_cloudwatch_metric(
                    metric,
                    point["Average"],
                    ts=point["Timestamp"].timestamp(),
                    source_instance=db_instance_id,
                )
            if points:
                results[metric] = points[-1]["Average"]
        return results

    def run_forever(self):
        target = self.db_cluster_id or self.db_instance_id
        mode = "cluster" if self.db_cluster_id else "instance"
        print(f"[whyslow] cloudwatch collector polling {mode} {target} every {self.interval}s")
        consecutive_failures = 0
        while True:
            try:
                results = self.poll_once()
                self.store.write_heartbeat(
                    "cloudwatch",
                    detail=(
                        f"writer={self.current_instance} metrics={sorted(results.keys())}"
                        if results
                        else f"writer={self.current_instance} no datapoints"
                    ),
                    expected_interval=self.interval,
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
                    print(f"[whyslow] cloudwatch poll failed ({consecutive_failures}x): {e}")
                time.sleep(min(self.interval * consecutive_failures, 300))
