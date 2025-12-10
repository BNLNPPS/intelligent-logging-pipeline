import redis
import json
import logging

# Set up logging
logger = logging.getLogger(__name__)

r = redis.Redis(host='redis', port=6379, decode_responses=True)

def push_sequence(cluster_ids):
    """Push a list of cluster IDs to Redis with error handling."""
    try:
        if cluster_ids:  # Only push if the list is not empty
            r.rpush("log_sequences", json.dumps(cluster_ids))
            logger.info(f"Pushed to Redis: {cluster_ids}")
    except redis.RedisError as e:
        logger.error(f"Failed to push to Redis: {e}")
    except Exception as e:
        logger.error(f"Unexpected error while pushing to Redis: {e}")
