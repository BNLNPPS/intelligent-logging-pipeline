import redis
import torch
from deeplog import DeepLog
from preprocessor import Preprocessor
import json
import os
import pandas as pd
import numpy as np
import time
import logging
import sys
import psutil

# =====================
# Logging Setup (stdout)
# =====================
logger = logging.getLogger("deeplog-service")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# =====================
# Configuration & Parameters
# =====================
k_top = int(os.getenv("k_top", "10"))
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

model_path = os.path.join(BASE_DIR, "deeplog_model.pth")
data_file = os.path.join(BASE_DIR, "redis_sequences.txt")
anomaly_results_file = os.path.join(BASE_DIR, "anomaly_results.txt")

# Redis Configuration (Use Env Vars for flexibility)
REDIS_HOST = os.getenv("REDIS_HOST", "redis.experiment-sphenix.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Initialize model and preprocessor
# Note: Input size 17 implies 16 unique events + 1 padding/unknown
model = DeepLog(input_size=17, hidden_size=64, output_size=17).to(device)
preprocessor = Preprocessor(length=20, timeout=float('inf'))

# =====================
# Redis Connection
# =====================
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    r.ping()
    logger.info(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
except redis.ConnectionError as e:
    logger.error(f"Redis connection failed: {e}")
    sys.exit(1)

# =====================
# Custom Predict
# =====================
def custom_predict(model, X, y, k):
    """Custom prediction function."""
    model.eval()
    with torch.no_grad():
        outputs = model(X)
        probabilities = torch.softmax(outputs, dim=1)
        top_k_probs, top_k_indices = torch.topk(probabilities, k, dim=1)
        return top_k_indices, top_k_probs

# =====================
# Resource and Metrics Helper
# =====================
def log_resource_usage():
    # CPU usage of the system
    cpu = psutil.cpu_percent(interval=None) 
    
    # Memory usage of THIS process specifically (RSS), not the whole system
    process = psutil.Process(os.getpid())
    mem = process.memory_info().rss / 1024 / 1024  # MB
    return cpu, mem

def process_sequence(seq, sequence_index, model, preprocessor, k_top):
    start_time = time.time()
    
    # Create DataFrame for preprocessor
    data_df = pd.DataFrame({
        'timestamp': np.arange(len(seq)), 
        'event': seq, 
        'machine': [0] * len(seq)
    })
    
    # Preprocess
    # Note: Returns empty tensors if sequence is shorter than window length
    X, y, _, _ = preprocessor.sequence(data_df)
    
    if len(X) == 0:
        # Sequence too short to predict
        return False, 0, 0, *log_resource_usage()

    X, y = X.to(device), y.to(device)

    # Prediction
    top_k_indices, _ = custom_predict(model, X, y, k=k_top)
    
    # Check Anomaly: Anomaly if the true label (y) is NOT in the top-k predictions
    # We check all windows created from this sequence. If ANY window is anomalous, mark sequence as anomaly.
    row_anomalies = [y[i].item() not in top_k_indices[i].cpu().numpy() for i in range(len(y))]
    is_anomaly = any(row_anomalies)

    # Metrics
    latency = time.time() - start_time
    throughput = 1 / latency if latency > 0 else 0
    cpu, mem = log_resource_usage()
    
    return is_anomaly, latency, throughput, cpu, mem

# =====================
# Training
# =====================
def train_initial_model():
    logger.info("Starting training phase...")
    sequences = []
    
    # Pull all available data for training
    while True:
        seq = r.lpop("log_sequences")
        if not seq:
            break
        try:
            parsed_seq = json.loads(seq)
            # Filter: Ensure sequence has data. 
            # Note: DeepLog needs len > window_size to create at least one X,y pair.
            if len(parsed_seq) > 1: 
                sequences.append(parsed_seq)
        except json.JSONDecodeError:
            continue

    if not sequences:
        logger.warning("No sequences found in Redis 'log_sequences' for training.")
        return False

    logger.info(f"Training on {len(sequences)} sequences.")

    # Save sequences to file (DeepLog preprocessor usually reads from file)
    with open(data_file, "w") as f:
        for seq in sequences:
            f.write(f"{' '.join(map(str, seq))}\n")
 
    # Train model
    try:
        X, y, _, _ = preprocessor.text(data_file)
        if len(X) == 0:
            logger.error("Preprocessing generated no training data (sequences too short?).")
            return False
            
        X, y = X.to(device), y.to(device)
        model.fit(X, y, epochs=100, batch_size=32)
        torch.save(model.state_dict(), model_path)
        logger.info(f"Model saved to {model_path}")
        return True
    except Exception as e:
        logger.error(f"Training error: {e}")
        return False

# =====================
# Anomaly Detection (Batch)
# =====================
def detect_anomalies():
    if not os.path.exists(model_path):
        logger.warning("Model not found. Skipping batch detection.")
        return [], []

    model.load_state_dict(torch.load(model_path))
    model.eval()
    
    anomalies = []
    metrics_summary = []
    sequence_index = 0

    logger.info("Starting batch anomaly detection on remaining Redis items...")

    with open(anomaly_results_file, "w") as f:
        while True:
            sequence = r.lpop("log_sequences")
            if not sequence:
                break # Queue is empty
                
            try:
                seq = json.loads(sequence)
                sequence_index += 1

                is_anomaly, latency, throughput, cpu, mem = process_sequence(seq, sequence_index, model, preprocessor, k_top)
                
                if is_anomaly:
                    anomalies.append((sequence_index, seq, is_anomaly))
                
                metrics_summary.append((sequence_index, latency, throughput, cpu, mem))

                status = 'Anomaly' if is_anomaly else 'Normal'
                f.write(f"Sequence {sequence_index}: {seq} - {status}\n")
                
                # Log only anomalies to reduce noise, or every Nth
                if is_anomaly or sequence_index % 100 == 0:
                    logger.info(f"Seq {sequence_index}: {status}, Latency={latency:.4f}s")
                    
            except json.JSONDecodeError:
                continue

    # Summary calculation
    if metrics_summary:
        avg_latency = np.mean([m[1] for m in metrics_summary])
        avg_throughput = np.mean([m[2] for m in metrics_summary])
        avg_cpu = np.mean([m[3] for m in metrics_summary])
        avg_mem = np.mean([m[4] for m in metrics_summary])
        logger.info(f"Batch Analysis Complete. Avg Latency: {avg_latency:.4f}s, Avg Mem: {avg_mem:.2f}MB")

    return anomalies, metrics_summary

# =====================
# Continuous Monitoring
# =====================
def monitor_redis(interval_sec=30):
    """
    Continuously monitor Redis. 
    Crucial Fix: Added sleep when queue is empty to prevent 100% CPU usage.
    """
    if not os.path.exists(model_path):
        logger.error("Model file not found. Cannot monitor.")
        return

    logger.info("Loading model for continuous monitoring...")
    model.load_state_dict(torch.load(model_path))
    model.eval()
    
    sequence_index = 0
    total_anomalies = 0
    total_sequences = 0
    
    # Metrics storage
    latency_list, throughput_list, cpu_list, mem_list = [], [], [], []
    last_log_time = time.time()

    logger.info(f"Monitoring started. Polling Redis list 'log_sequences'...")

    with open(anomaly_results_file, "a") as f:
        while True:
            try:
                sequence = r.lpop("log_sequences")
                
                # === FIX: Prevent CPU spinning ===
                if not sequence:
                    time.sleep(1) # Wait for new data
                    continue

                seq = json.loads(sequence)
                sequence_index += 1

                # Process
                is_anomaly, latency, throughput, cpu, mem = process_sequence(seq, sequence_index, model, preprocessor, k_top)

                # Log to file
                status = 'Anomaly' if is_anomaly else 'Normal'
                f.write(f"Sequence {sequence_index}: {seq} - {status}\n")
                f.flush() # Ensure it's written immediately

                # Update stats
                total_sequences += 1
                if is_anomaly:
                    total_anomalies += 1
                
                latency_list.append(latency)
                throughput_list.append(throughput)
                cpu_list.append(cpu)
                mem_list.append(mem)

                # Periodic Summary Logging
                current_time = time.time()
                if current_time - last_log_time >= interval_sec:
                    avg_latency = np.mean(latency_list) if latency_list else 0
                    avg_cpu = np.mean(cpu_list) if cpu_list else 0
                    avg_mem = np.mean(mem_list) if mem_list else 0

                    logger.info(f"--- Summary (last {interval_sec}s) ---")
                    logger.info(f"Processed: {len(latency_list)} | Total Processed: {total_sequences}")
                    logger.info(f"Anomalies: {total_anomalies} (Global)")
                    logger.info(f"Avg Latency: {avg_latency:.4f}s | Avg Mem: {avg_mem:.2f}MB")
                    logger.info(f"------------------------------------")
                    
                    # Reset interval lists
                    latency_list, throughput_list, cpu_list, mem_list = [], [], [], []
                    last_log_time = current_time

            except (json.JSONDecodeError, redis.RedisError) as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(1)
                continue
            except KeyboardInterrupt:
                logger.info("Stopping monitoring...")
                break

# =====================
# Main Entry
# =====================
if __name__ == "__main__":
    # 1. Train if needed
    if not os.path.exists(model_path) or os.path.getsize(model_path) == 0:
        logger.info("Model not found. Attempting to train from Redis data...")
        success = train_initial_model()
        if not success:
            logger.error("Training failed or no data available. Exiting.")
            # We exit here because we can't detect without a model
            sys.exit(1)
    else:
        logger.info("Found existing model. Skipping training.")

    # 2. Process any immediate backlog
    anomalies, _ = detect_anomalies()
    if anomalies:
        logger.info(f"Backlog processing found {len(anomalies)} anomalies.")

    # 3. Switch to continuous monitoring
    logger.info("Switching to Real-time Monitoring Mode...")
    monitor_redis(interval_sec=15)