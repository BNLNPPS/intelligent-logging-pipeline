import os
import json
import torch
import numpy as np
import pandas as pd
from sklearn.metrics import precision_score, recall_score, f1_score, classification_report
from deeplog import DeepLog
from preprocessor import Preprocessor

# ============================================================
# Paths and device
# ============================================================
BASE_DIR = os.getcwd()
MODEL_PATH = os.path.join(BASE_DIR, "deeplog_local_safe.pth")
DATA_FILE = os.path.join(BASE_DIR, "training_sequences.txt")
JSON_FILE = os.path.join(BASE_DIR, "local_sequences.json")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ============================================================
# Hyperparameters
# ============================================================
HIDDEN_SIZE = 128
NUM_LAYERS = 3
EPOCHS = 30
BATCH_SIZE = 32
TOP_K = 20
ANOMALY_THRESHOLD = 0.30
WINDOW_LENGTH = 20

# ============================================================
# Load sequences safely
# ============================================================
def load_sequences():
    if not os.path.exists(JSON_FILE):
        raise FileNotFoundError(f"{JSON_FILE} not found")
    with open(JSON_FILE) as f:
        data = json.load(f)

    # Convert dict → list if needed
    if isinstance(data, dict):
        data = list(data.values())

    cleaned = []
    for item in data:
        if isinstance(item, dict) and "seq" in item and "label" in item:
            cleaned.append((item["seq"], int(item["label"])))
        elif isinstance(item, list):
            # unlabeled format, default label=0
            cleaned.append((item, 0))
        else:
            print("Skipping malformed item:", item)
    return cleaned

# ============================================================
# Determine input_size dynamically
# ============================================================
def infer_input_size(sequences):
    max_id = 0
    for seq, _ in sequences:
        if len(seq) > 0:
            max_id = max(max_id, max(seq))
    return max_id + 1  # because IDs start at 0

# ============================================================
# Process sequence for anomaly detection
# ============================================================
def detect_anomaly(seq, model, preprocessor, threshold):
    df = pd.DataFrame({
        "timestamp": np.arange(len(seq)),
        "event": seq,
        "machine": [0]*len(seq)
    })

    X, y, _, _ = preprocessor.sequence(df)
    if len(X) == 0:
        return False

    # --- Clip event IDs to prevent one-hot errors ---
    X = torch.clamp(X, max=model.input_size - 1).to(device)
    y = y.to(device)

    with torch.no_grad():
        outputs = model(X)
        probs = torch.softmax(outputs, dim=1)
        true_probs = probs[range(len(y)), y].cpu().numpy()

    # Anomaly if probability of true label < threshold
    return any(true_probs < threshold)

# ============================================================
# Train model
# ============================================================
def train_model(model, preprocessor, sequences):
    # Save sequences to text file for preprocessor
    with open(DATA_FILE, "w") as f:
        for seq, _ in sequences:
            f.write(" ".join(map(str, seq)) + "\n")

    X, y, _, _ = preprocessor.text(DATA_FILE)
    X, y = X.to(device), y.to(device)

    print("Training model...")
    model.fit(X, y, epochs=EPOCHS, batch_size=BATCH_SIZE)
    torch.save(model.state_dict(), MODEL_PATH)
    print("Model trained & saved:", MODEL_PATH)

# ============================================================
# Evaluate model
# ============================================================
def evaluate_model(model, preprocessor, sequences):
    print("Evaluating model...")
    y_true = []
    y_pred = []

    for seq, label in sequences:
        pred = detect_anomaly(seq, model, preprocessor, ANOMALY_THRESHOLD)
        pred_label = 1 if pred else 0
        y_true.append(label)
        y_pred.append(pred_label)
        print(f"SEQ={seq} | TRUE={label} | PRED={pred_label}")

    precision = precision_score(y_true, y_pred, zero_division=0)
    recall = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)

    print("\n===== METRICS =====")
    print(f"Precision: {precision:.4f}")
    print(f"Recall:    {recall:.4f}")
    print(f"F1-score:  {f1:.4f}")
    print("====================\n")
    print("CLASSIFICATION REPORT\n")
    print(classification_report(y_true, y_pred, digits=4))

# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    sequences = load_sequences()
    input_size = infer_input_size(sequences)
    print(f"Inferred input_size: {input_size}")

    model = DeepLog(
        input_size=input_size,
        hidden_size=HIDDEN_SIZE,
        output_size=input_size,
        num_layers=NUM_LAYERS
    ).to(device)

    preprocessor = Preprocessor(length=WINDOW_LENGTH, timeout=float('inf'))

    # Train
    train_model(model, preprocessor, sequences)

    # Load trained model and evaluate
    model.load_state_dict(torch.load(MODEL_PATH))
    model.eval()
    evaluate_model(model, preprocessor, sequences)
