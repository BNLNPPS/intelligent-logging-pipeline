import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.nn import Module

class DeepLog(Module):

    def __init__(self, input_size, hidden_size, output_size, num_layers=2):
        """DeepLog model used for training and predicting logs.

            Parameters
            ----------
            input_size : int
                Dimension of input layer.

            hidden_size : int
                Dimension of hidden layer.

            output_size : int
                Dimension of output layer.

            num_layers : int, default=2
                Number of hidden layers, i.e. stacked LSTM modules.
            """
        # Initialise nn.Module
        super(DeepLog, self).__init__()

        # Store input parameters
        self.input_size  = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.num_layers  = num_layers

        # Initialise model layers
        self.lstm    = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
        self.out     = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=-1)

    ########################################################################
    #                       Forward through network                        #
    ########################################################################

    def forward(self, X):
        """
        Forward pass for DeepLog.
        Clips event IDs to avoid one-hot errors.
        """
        # Clip event IDs to valid range
        X = torch.clamp(X, min=0, max=self.input_size - 1).to(torch.int64)

        # One-hot encode
        X = F.one_hot(X, num_classes=self.input_size).to(torch.float)

        # Initialize hidden states
        hidden = self._get_initial_state(X)
        state  = self._get_initial_state(X)

        # LSTM forward
        out, hidden = self.lstm(X, (hidden, state))
        out = self.out(out[:, -1, :])
        out = self.softmax(out)
        return out


    ########################################################################
    #                            Predict method                            #
    ########################################################################

    def predict(self, X, y=None, k=1, variable=False, verbose=True):
        """Predict the k most likely output values

            Parameters
            ----------
            X : torch.Tensor of shape=(n_samples, seq_len)
                Input of sequences, these will be one-hot encoded to an array of
                shape=(n_samples, seq_len, input_size)

            y : Ignored
                Ignored

            k : int, default=1
                Number of output items to generate

            variable : boolean, default=False
                If True, predict inputs of different sequence lengths

            verbose : boolean, default=True
                If True, print output

            Returns
            -------
            result : torch.Tensor of shape=(n_samples, k)
                k most likely outputs

            confidence : torch.Tensor of shape=(n_samples, k)
                Confidence levels for each output
            """
        # Get the predictions
        result = super().predict(X, variable=variable, verbose=verbose)
        # Get the probabilities from the log probabilities
        result = result.exp()
        # Compute k most likely outputs
        confidence, result = result.topk(k)
        # Return result
        return result, confidence

    ########################################################################
    #                             I/O methods                              #
    ########################################################################

    def save(self, outfile):
        """Save model to output file.

            Parameters
            ----------
            outfile : string
                File to output model.
            """
        # Save to output file
        torch.save(self.state_dict(), outfile)

    @classmethod
    def load(cls, infile, device=None):
        """Load model from input file.

            Parameters
            ----------
            infile : string
                File from which to load model.
            """
        # Load state dictionary
        state_dict = torch.load(infile, map_location=device)

        print(state_dict.keys())

        # Get input variables from state_dict
        input_size  = state_dict.get('lstm.weight_ih_l0').shape[1]
        hidden_size = state_dict.get('lstm.weight_hh_l0').shape[1]
        output_size = input_size
        num_layers  = (len(state_dict) - 2) // 4

        # Create ContextBuilder
        result = cls(
            input_size  = input_size,
            hidden_size = hidden_size,
            output_size = output_size,
            num_layers  = num_layers,
        )

        # Cast to device if necessary
        if device is not None: result = result.to(device)

        # Set trained parameters
        result.load_state_dict(state_dict)

        # Return result
        return result

    ########################################################################
    #                         Auxiliary functions                          #
    ########################################################################

    def _get_initial_state(self, X):
        """Return a given hidden state for X."""
        # Return tensor of correct shape as device
        return torch.zeros(
            self.num_layers ,
            X.size(0)       ,
            self.hidden_size
        ).to(X.device)
    ########################################################################
    #                             Fit method                               #
    ########################################################################

    def fit(self, X, y, epochs=10, lr=0.001, batch_size=32, verbose=True, device=None):
        """Train DeepLog model.

            Parameters
            ----------
            X : torch.Tensor of shape=(n_samples, seq_len)
                Input sequences.

            y : torch.Tensor of shape=(n_samples,)
                Target labels.

            epochs : int
                Number of training epochs.

            lr : float
                Learning rate.

            batch_size : int
                Mini-batch size.

            verbose : bool
                If True, print training loss per epoch.

            device : torch.device
                Device to use ("cpu" or "cuda").
        """
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.to(device)

        # Loss & optimizer
        criterion = nn.CrossEntropyLoss()
        optimizer = optim.Adam(self.parameters(), lr=lr)

        dataset = torch.utils.data.TensorDataset(X, y)
        dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True)

        for epoch in range(epochs):
            self.train()
            total_loss = 0
            for batch_X, batch_y in dataloader:
                batch_X, batch_y = batch_X.to(device), batch_y.to(device)

                optimizer.zero_grad()
                outputs = self(batch_X)
                loss = criterion(outputs, batch_y)
                loss.backward()
                optimizer.step()

                total_loss += loss.item()

            if verbose:
                avg_loss = total_loss / len(dataloader)
                print(f"Epoch [{epoch+1}/{epochs}] Loss: {avg_loss:.4f}")
