import subprocess
import json
import re
import logging
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig
from drain3.file_persistence import FilePersistence
from redis_store import push_sequence

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

DRAIN3_PERSISTENCE_FILE = "/var/drain3/drain3_state.bin"
DRAIN3_CONFIG_FILE = "./drain3.ini"

TIMESTAMP_REGEX = r'\[\d{2}/\w+/\d{4}:\d{2}:\d{2}:\d{2} \+\d{4}\]\s*'

def query_loki():
    """Query logs from Loki using logcli, returning raw log lines."""
    cmd = (
        'logcli --addr="http://loki.experiment-sphenix.svc.cluster.local:3100" query '
        '\'{job="django"}\' --limit=20 --output jsonl | jq -r \'.line\''
    )
    logger.info(f"Executing logcli command: {cmd}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        log_lines = [line for line in result.stdout.strip().split('\n') if line]
        logger.info(f"Retrieved {len(log_lines)} log lines from Loki")
        logger.debug(f"First 3 raw lines from Loki: {log_lines[:3]}")  # <--
        return log_lines
    except subprocess.CalledProcessError as e:
        logger.error(f"Error querying Loki: {e.stderr}")
        return []

def preprocess_log_line(log_line):
    """Remove gunicorn timestamp and normalize the log line for Drain3."""
    cleaned = re.sub(TIMESTAMP_REGEX, '', log_line).strip()
    logger.debug(f"  before: {repr(log_line)}")    # <--
    logger.debug(f"  after:  {repr(cleaned)}")     # <--
    return cleaned

def process_with_drain3(log_lines):
    """Process logs with Drain3, extract cluster IDs, and send to Redis in batches."""
    persistence = FilePersistence(DRAIN3_PERSISTENCE_FILE)
    config = TemplateMinerConfig()
    config.load(DRAIN3_CONFIG_FILE)
    template_miner = TemplateMiner(persistence, config)
    logger.info("Drain3 started with 'FILE' persistence")

    batch = []
    batch_size = 20
    parse_ok = 0    # <--
    parse_skip = 0  # <--

    for log_line in log_lines:
        cleaned = preprocess_log_line(log_line)
        if not cleaned:
            logger.warning(f"Skipping empty line after preprocessing: {repr(log_line)}")
            parse_skip += 1  # <--
            continue

        result = template_miner.add_log_message(cleaned)
        if result is None:
            logger.warning(f"Drain3 returned None for: {repr(cleaned)}")  # <--
            parse_skip += 1                                                # <--
            continue

        parse_ok += 1  # <--
        cluster_id = result["cluster_id"]
        logger.info(f"Cluster ID: {cluster_id} | Template: {result['template_mined']}")

        params = template_miner.extract_parameters(result["template_mined"], cleaned)
        logger.info(f"Parameters: {params}")

        batch.append(cluster_id)
        if len(batch) >= batch_size:
            push_sequence(batch)
            logger.info(f"Pushed batch to Redis: {batch}")
            batch = []

    # <--
    logger.info(f"Parsing summary: {parse_ok} processed, {parse_skip} skipped out of {len(log_lines)} total")

    if batch:
        push_sequence(batch)
        logger.info(f"Pushed final batch to Redis: {batch}")

    logger.info("Mined clusters:")
    for cluster in template_miner.drain.clusters:
        logger.info(f"ID={cluster.cluster_id} : size={cluster.size} : {cluster.get_template()}")


if __name__ == "__main__":
    log_lines = query_loki()
    if log_lines:
        process_with_drain3(log_lines)
    else:
        logger.info("No logs retrieved from Loki")