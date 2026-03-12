

import redis
import json

r = redis.Redis(host='redis.experiment-sphenix.svc.cluster.local', port=6379, decode_responses=True)

def push_sequence(cluster_ids):
    """Push a list of cluster IDs to Redis."""
    r.rpush("log_sequences", json.dumps(cluster_ids))
