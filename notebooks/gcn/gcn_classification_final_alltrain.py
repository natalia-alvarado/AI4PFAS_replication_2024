import sys
sys.path.append('../..')

# Imports
import os
from pathlib import Path
import argparse

import pandas as pd
import numpy as np

import setuptools.dist
from src import models

from rdkit import Chem
from rdkit.Chem import Descriptors
from sklearn.preprocessing import StandardScaler
import sklearn.metrics

from sklearn.model_selection import KFold, StratifiedKFold
import torch
from torch_geometric.data import Data
from collections import OrderedDict
from torch_geometric.data import DataLoader

import math
import torch.nn as nn
# Function to initialize weights randomly
import torch.nn.init as init
import torch.nn.functional as F
import torch_geometric.nn as gnn

import matplotlib.pyplot as plt
from sklearn.metrics import ConfusionMatrixDisplay
from sklearn.metrics import r2_score, mean_absolute_error, root_mean_squared_error, mean_squared_error
from sklearn.model_selection import train_test_split
from scipy.stats import spearmanr
import seaborn as sns

import pickle
import random

def load_data(task_type, random_state = 42):
    # Load the dataset
    ldtoxdb = pd.read_csv('../../data/full_dataset.csv')

    # Filter the dataset based on 'pfas_like'
    df_true = ldtoxdb[ldtoxdb['is_pfas_like'] == True]
    df_false = ldtoxdb[ldtoxdb['is_pfas_like'] == False]
        
    train_true, val_true = train_test_split(df_true, test_size=0.1, random_state=random_state, stratify=df_true['epa'])

    train_false, val_false = train_test_split(df_false, test_size=0.2, random_state=random_state, stratify=df_false['epa'])  # 80%-20% split
    
    # Combine the splits
    train_data = pd.concat([train_true, train_false], axis=0)
    val_data = pd.concat([val_true, val_false], axis=0)

    # Extract the corresponding features (smiles) and target variable (y) for the splits
    data_x = train_data.smiles.to_numpy().reshape(-1, 1)
    val_x = val_data.smiles.to_numpy().reshape(-1, 1)
    
    # For classification task, we use the 'epa' column as target, for regression we use 'neglogld50'
    if task_type == 'regression':
        data_y = train_data.neglogld50.to_numpy().reshape(-1, 1)
        val_y = val_data.neglogld50.to_numpy().reshape(-1, 1)
    elif task_type == 'classification':
        data_y = train_data.epa.to_numpy().reshape(-1, 1)
        val_y = val_data.epa.to_numpy().reshape(-1, 1)

    # Extract the 'epa' column for classification (used for stratified splitting)
    epa = train_data.epa.to_numpy().reshape(-1, 1)
    val_epa = val_data.epa.to_numpy().reshape(-1, 1)
    
    # Return the final split data
    return data_x, val_x, data_y, val_y, epa, val_epa
	
# Graph
possible_atom_list = ['S', 'Si', 'F', 'O',
                      'C', 'I', 'P', 'Cl', 'Br', 'N', 'Unknown']

def one_of_k_encoding(x, allowable_set):
    if x not in allowable_set:
        raise Exception("input {0} not in allowable set{1}:".format(
            x, allowable_set))
    return list(map(lambda s: x == s, allowable_set))

def one_of_k_encoding_unk(x, allowable_set):
    """Maps inputs not in the allowable set to the last element."""
    if x not in allowable_set:
        x = allowable_set[-1]
    return list(map(lambda s: x == s, allowable_set))

def atom_features(atom):
    results = one_of_k_encoding_unk(atom.GetSymbol(), possible_atom_list) + \
        one_of_k_encoding(atom.GetDegree(),
                          [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) + \
        one_of_k_encoding_unk(atom.GetImplicitValence(), [0, 1, 2, 3, 4, 5, 6]) + \
        [atom.GetFormalCharge(), atom.GetNumRadicalElectrons()] + \
        one_of_k_encoding_unk(atom.GetHybridization(), [
            Chem.rdchem.HybridizationType.SP, Chem.rdchem.HybridizationType.SP2,
            Chem.rdchem.HybridizationType.SP3, Chem.rdchem.HybridizationType.
            SP3D, Chem.rdchem.HybridizationType.SP3D2
        ]) + [atom.GetIsAromatic()]
    return np.array(results).astype(np.float32)


def bond_features(bond):
    bt = bond.GetBondType()
    bond_feats = [
        bt == Chem.rdchem.BondType.SINGLE, bt == Chem.rdchem.BondType.DOUBLE,
        bt == Chem.rdchem.BondType.TRIPLE, bt == Chem.rdchem.BondType.AROMATIC,
        bond.GetIsConjugated(),
        bond.IsInRing()]
    return np.array(bond_feats).astype(np.float32)


def n_atom_features():
    atom = Chem.MolFromSmiles('C').GetAtomWithIdx(0)
    return len(atom_features(atom))

def n_bond_features():
    bond = Chem.MolFromSmiles('CC').GetBondWithIdx(0)
    return len(bond_features(bond))

def get_bond_pair(mol):
    bonds = mol.GetBonds()
    res = [[], []]
    for bond in bonds:
        res[0] += [bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()]
        res[1] += [bond.GetEndAtomIdx(), bond.GetBeginAtomIdx()]
    return res

def mol2torchdata(mol):
    atoms = mol.GetAtoms()
    bonds = mol.GetBonds()
    node_f = [atom_features(atom) for atom in atoms]
    edge_index = get_bond_pair(mol)
    edge_attr = [bond_features(bond) for bond in bonds]
    for bond in bonds:
        edge_attr.append(bond_features(bond))
    data = Data(x=torch.tensor(node_f, dtype=torch.float),
                edge_index=torch.tensor(edge_index, dtype=torch.long),
                edge_attr=torch.tensor(edge_attr, dtype=torch.float)
                )
    return data

# Training
def clear_model(model):
    del model
    torch.cuda.empty_cache()


def get_dataloader(df, index, target, mol_column, batch_size, y_scaler):
    y_values = df.loc[index, target].values.reshape(-1, 1)
    y = y_scaler.transform(y_values).ravel().astype(np.float32)
    x = df.loc[index, mol_column].progress_apply(mol2torchdata).tolist()
    for data, y_i in zip(x, y):
        data.y = torch.tensor([y_i], dtype=torch.float)
    data_loader = DataLoader(x, batch_size=batch_size,
                             shuffle=True, drop_last=False)
    return data_loader

def reg_stats(y_true, y_pred):
    r2 = sklearn.metrics.r2_score(y_true, y_pred)
    mae = sklearn.metrics.mean_absolute_error(y_true, y_pred)
    return r2, mae

def train_step(model, data_loader, optimizer, scheduler, device):
    model.train()
    total_loss = 0
    
    for data in data_loader:
        data = data.to(device)
        optimizer.zero_grad()
        output = model(data)
        
        # Compute CE loss for multi-label classification
        loss = GCN.criterion(output, data.y)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()
        
    avg_loss = total_loss / len(data_loader)
    if scheduler:
        scheduler.step(avg_loss)
    return avg_loss

def compute_val_loss(model, data_loader, device):
        model.eval()  # Set model to evaluation mode
        total_loss = 0

        with torch.no_grad():
            for data in data_loader:
                data = data.to(device)
                output = model(data)  # Forward pass
                loss = GCN.criterion(output, data.y)  # Compute loss
                total_loss += loss.item()

        # Average loss for the validation set
        avg_loss = total_loss / len(data_loader)
        return avg_loss

def get_embeddings(model, data_loader, y_scaler, device):
    with torch.no_grad():
        model.eval()
        z = []
        y = []
        for data in data_loader:
            data = data.to(device)
            z_data = model.forward_gnn(data)
            y_data = model.pred(z_data)
            y.append(y_data.cpu().numpy())
            z.append(z_data.cpu().numpy())

        y = y_scaler.inverse_transform(np.vstack(y).reshape(-1, 1)).ravel()
        z = np.vstack(z)
    return z, y
	
# Molan Model for GCN
def str2act(act):
    activations = {'relu': nn.ReLU(), 'selu': nn.SELU(), 'celu': nn.CELU(
    ), 'softplus': nn.Softplus(), 'softmax': nn.Softmax(), 'sigmoid': nn.Sigmoid()}
    return activations[act]


def str2funct_act(act):
    activations = {'relu': F.relu, 'selu': F.selu, 'celu': F.celu,
                   'softplus': F.softplus, 'softmax': F.softmax, 'sigmoid': F.sigmoid}
    return activations[act]


def net_pattern(n_layers, base_size, ratio, maxv=1024):
    return [int(min(max(math.ceil(base_size * (ratio**i)), 0), maxv)) for i in range(0, n_layers)]


def make_mlp(start_dim, n_layers, ratio, act, batchnorm, dropout):
    layer_sizes = net_pattern(n_layers + 1, start_dim, ratio)
    layers = []
    for index in range(n_layers):
        layers.append(nn.Linear(layer_sizes[index], layer_sizes[index + 1]))
        layers.append(str2act(act))
        if batchnorm:
            layers.append(nn.BatchNorm1d(layer_sizes[index + 1]))
        if dropout > 0.0:
            layers.append(nn.Dropout(dropout))

    return nn.Sequential(*layers), layer_sizes[-1]


class molan_model_GCN(torch.nn.Module):
    def __init__(self, hparams, node_dim, edge_dim, num_classes):
        super(molan_model_GCN, self).__init__()

        self.node_dim = node_dim
        self.edge_dim = edge_dim
        self.hparams = hparams
        self.output_dim = num_classes  # Number of classes for multi-label output

        # Linear atom embedding
        self.linatoms = torch.nn.Linear(
            self.node_dim, hparams['conv_base_size'])

        # Graph Convolution
        emb_dim = hparams['emb_dim']
        conv_dims = net_pattern(hparams['conv_n_layers'],
                                hparams['conv_base_size'],
                                hparams['conv_ratio']) + [emb_dim]
        conv_layers = []
        for index in range(hparams['conv_n_layers']):
            conv_layers.append(gnn.GCNConv(
                conv_dims[index], conv_dims[index + 1], cached=False))

        self.graph_conv = nn.ModuleList(conv_layers)
        if self.hparams['conv_batchnorm']:
            self.bn = nn.ModuleList([nn.BatchNorm1d(dim)
                                     for dim in conv_dims[1:]])
        # Graph embedding
        if hparams['emb_set2set']:
            self.graph_emb = gnn.Set2Set(emb_dim, processing_steps=3)
            emb_dim = emb_dim * 2
        else:
            self.graph_emb = nn.Sequential(nn.Linear(emb_dim, emb_dim),
                                           str2act(hparams['emb_act']))

        # Build mlp
        self.using_mlp = hparams['mlp_layers'] > 0
        if self.using_mlp:
            self.mlp, last_dim = make_mlp(emb_dim,
                                          hparams['mlp_layers'],
                                          hparams['mlp_dim_ratio'],
                                          hparams['mlp_act'],
                                          hparams['mlp_batchnorm'],
                                          hparams['mlp_dropout'])
        else:
            last_dim = emb_dim

        # Prediction layer for multi-label classification
        self.pred = nn.Linear(last_dim, self.output_dim)

        # placeholder for the gradients
        self.gradients = None

    # hook for the gradients of the activations
    def activations_hook(self, grad):
        self.gradients = grad

    # method for the gradient extraction
    def get_activations_gradient(self):
        return self.gradients

    # method for the activation extraction
    def get_activations(self, data):
        x, edge_index = data.x, data.edge_index
        # Linear atom embedding
        x = self.linatoms(x)
        # GCN part
        for index in range(self.hparams['conv_n_layers']):
            x = self.graph_conv[index](x, edge_index)
            x = str2funct_act(self.hparams['conv_act'])(x)
        return x

    def forward_gnn(self, data, gradcam=False):
        x, edge_index = data.x, data.edge_index
        # Linear atom embedding
        x = self.linatoms(x)
        # GCN part
        for index in range(self.hparams['conv_n_layers']):
            x = self.graph_conv[index](x, edge_index)
            x = str2funct_act(self.hparams['conv_act'])(x)
            if gradcam and index == self.hparams['conv_n_layers'] - 1:
                # register the hook
                x.register_hook(self.activations_hook)

            if self.hparams['conv_batchnorm']:
                x = self.bn[index](x)

        # Graph embedding
        if self.hparams['emb_set2set']:
            x = self.graph_emb(x, data.batch)
        else:
            x = self.graph_emb(x)
            x = gnn.global_add_pool(x, data.batch)
            GCN.graph_embs.append(x)
        # NNet
        if self.using_mlp:
            x = self.mlp(x)
        return x

    def forward(self, data, gradcam=False):
        x = self.forward_gnn(data, gradcam)
        
        # Prediction
        x = self.pred(x) 
 
        return x 

		
# GCN setup
class GCN:
    # Some code here taken directly from MOLAN
    seed = 42
    conv_n_layers = 5
    conv_base_size = 64
    conv_ratio = 1.25
    conv_batchnorm = True
    conv_act = 'relu'
    emb_dim = 100
    emb_set2set = False
    emb_act = 'softmax'
    mlp_layers = 2
    mlp_dim_ratio = 0.5
    mlp_dropout = 0.15306049825909776
    mlp_act = 'relu'
    mlp_batchnorm = True
    residual = False
    learning_rate = 0.001
    batch_size = 64
    epochs = 200
    node_dim = n_atom_features()
    edge_dim = n_bond_features()
    criterion = nn.CrossEntropyLoss()
    num_classes = 4
    graph_embs = []
    val_data = None
    val_loss = []
    train_loss = []

    def fit(self, x_train, y_train):
        torch.manual_seed(self.seed)

        hparams = OrderedDict([('conv_n_layers', self.conv_n_layers), ('conv_base_size', self.conv_base_size),
                        ('conv_ratio', self.conv_ratio), ('conv_batchnorm', self.conv_batchnorm),
                        ('conv_act', self.conv_act), ('emb_dim', self.emb_dim),
                        ('emb_set2set', self.emb_set2set), ('emb_act', self.emb_act),
                        ('mlp_layers', self.mlp_layers), ('mlp_dim_ratio', self.mlp_dim_ratio),
                        ('mlp_dropout', self.mlp_dropout), ('mlp_act', self.mlp_act),
                        ('mlp_batchnorm', self.mlp_batchnorm), ('residual', self.residual)])

        hparams['lr'] = self.learning_rate
        hparams['batch_size'] = self.batch_size
        hparams['model'] = 'GCN'

        x_train = [mol2torchdata(Chem.MolFromSmiles(smile)) for smile in x_train.flatten()]

        for data, y in zip(x_train, y_train):
            data.y = torch.tensor(y, dtype=torch.long)

        loader = DataLoader(x_train, batch_size=self.batch_size,
            shuffle=False, drop_last=False)
        

        x_val, y_val = self.val_data
        x_val = [mol2torchdata(Chem.MolFromSmiles(smile)) for smile in x_val.flatten()]

        for val_data, val_y in zip(x_val, y_val):
            val_data.y = torch.tensor(val_y, dtype=torch.long)

        val_loader = DataLoader(x_val, batch_size=self.batch_size,
            shuffle=False, drop_last=False)

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = molan_model_GCN(hparams, self.node_dim, self.edge_dim, self.num_classes).to(self.device)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=hparams['lr'])
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,
                        mode = 'min',
                        factor = 0.5,
                        patience = 20,
                        verbose = True)
               
        for i in range(self.epochs):
            print('Step %d/%d' % (i+1, self.epochs))
            self.train_loss.append([train_step(self.model, loader, optimizer, scheduler, self.device)])
            self.val_loss.append([compute_val_loss(self.model,val_loader,self.device)])

    def predict(self, x_in):
        # Prepare input data for prediction
        x_in = [mol2torchdata(Chem.MolFromSmiles(smile)) for smile in x_in.flatten()]
        
        # Data loader without shuffling and with single batch size
        loader = DataLoader(x_in, batch_size=1, shuffle=False, drop_last=False)

        results = []

        with torch.no_grad():
            self.model.eval()  # Set model to evaluation mode
            
            for data in loader:
                data = data.to(self.device)
                output = self.model(data)
                
                # Get the index of the class with the highest probability
                predicted_class = torch.argmax(output, dim=1)
                
                # Convert to numpy and add to results
                results.append(predicted_class.cpu().numpy())
        
        # Return the results as a numpy array
        return np.array(results).reshape(-1, 1)

    def save_weights(self, fn):
        torch.save(self.model.state_dict(), fn)

    def load_weights(self, fn):
        hparams = OrderedDict([('conv_n_layers', self.conv_n_layers), ('conv_base_size', self.conv_base_size),
                        ('conv_ratio', self.conv_ratio), ('conv_batchnorm', self.conv_batchnorm),
                        ('conv_act', self.conv_act), ('emb_dim', self.emb_dim),
                        ('emb_set2set', self.emb_set2set), ('emb_act', self.emb_act),
                        ('mlp_layers', self.mlp_layers), ('mlp_dim_ratio', self.mlp_dim_ratio),
                        ('mlp_dropout', self.mlp_dropout), ('mlp_act', self.mlp_act),
                        ('mlp_batchnorm', self.mlp_batchnorm), ('residual', self.residual)])

        hparams['lr'] = self.learning_rate
        hparams['batch_size'] = self.batch_size
        hparams['model'] = 'GCN'

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = molan_model_GCN.GCN(hparams, self.node_dim, self.edge_dim)
        self.model.load_state_dict(torch.load(fn))

# Experimental setup
class LD50UnitConverter():
    def convert_to_mgkg(self, neglogld50s, smiles):

        for neglogld50, smile in zip(neglogld50s, smiles):
            molwt = Descriptors.MolWt(Chem.MolFromSmiles(smile[0]))
            yield (10**(-1*neglogld50[0]))*1000*molwt


    def convert_to_epa(self, neglogld50s, smiles):
        mgkg = list(self.convert_to_mgkg(neglogld50s=neglogld50s, smiles=smiles))

        return pd.cut(mgkg, labels=(0,1,2,3), bins=(-np.inf,50,500,5000, np.inf))

class CrossValidator():
    def __init__(self, splits=5, sampling_type='random', seed=None):
        self.sampling_stratified = sampling_type == 'stratified'
        self.splits = splits
        self.seed = seed  # Store the seed

    def get_folds(self, x, y, c=None):
        if self.sampling_stratified:
            kf = StratifiedKFold(n_splits=self.splits, shuffle=True, random_state=self.seed)  # Set the random_state
            for train_index, val_index in kf.split(x, c):
                x_train, x_val = x[train_index], x[val_index]
                y_train, y_val = y[train_index], y[val_index]
                train = (x_train, y_train, x_train)  # x_train twice so I don't have to move more things
                test = (x_val, y_val, x_val)
                yield (train, test)
        else:
            kf = KFold(n_splits=self.splits, shuffle=True, random_state=self.seed)  # Set the random_state
            for train_index, val_index in kf.split(x, y):
                x_train, x_val = x[train_index], x[val_index]
                y_train, y_val = y[train_index], y[val_index]
                train = (x_train, y_train, x_train)  # x_train twice so I don't have to move more things
                test = (x_val, y_val, x_val)
                yield (train, test)
				
# Predict and analize
def variance_of_residuals(y_true, y_predicted):
    # Calculate the residuals (errors)
    residuals = np.array(y_true) - np.array(y_predicted)
    
    # Compute the variance using the formula for variance
    mean_residual = np.mean(residuals)
    return np.mean((residuals - mean_residual) ** 2)

def spearman_correlation_scorer(actual_y, pred_y):
    rho, _ = spearmanr(actual_y, pred_y)
    return rho

def initialize_weights(model, seed):
    '''Takes in a module and initializes all linear layers with weight
    values taken from a normal distribution.
    from https://stackoverflow.com/a/55546528
    '''
    torch.manual_seed(seed)
    np.random.seed(seed)

    classname = model.__class__.__name__
    # for every Linear layer in a model..
    if classname.find('Linear') != -1:
        # get the number of the inputs
        n = model.in_features
        y = 1.0/np.sqrt(n)
        model.weight.data.uniform_(-y, y)
        model.bias.data.fill_(0)

def convert_to_mgkg(neglogld50s, smiles):
    mgkg_values = []
    for neglogld50, smile in zip(neglogld50s, smiles):
        molwt = Descriptors.MolWt(Chem.MolFromSmiles(smile))
        mgkg = (10**(-1*neglogld50)) * 1000 * molwt
        mgkg_values.append(mgkg)
    return mgkg_values

# Function to convert mg/kg values to EPA categories
def convert_to_epa(neglog_values, smiles):
    mgkg_values = convert_to_mgkg(neglog_values, smiles)
    epa_categories = pd.cut(mgkg_values, labels=[0,1,2,3], bins=[-np.inf, 50, 500, 5000, np.inf])
    return epa_categories
	
if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Run GCN model")
    
    # Parse arguments
    args = parser.parse_args()

    folder_paths = ['../../data/replication_gcn/final_model_test','../../data/replication_gcn/final_model_test/loss','../../data/replication_gcn/final_model_test/graph_embeddings','../../data/replication_gcn/final_model_test/benchmark-models','../../data/replication_gcn/final_model_test/chkpts']

    for path in folder_paths:
        # Check if the folder exists, and create it if it does not
        if not os.path.exists(path):
            os.makedirs(path)
            print(f"Folder '{path}' created.")
        else:
            print(f"Folder '{path}' already exists.")
	
	# GCN
    benchmark = {'model': GCN, 'encoding': 'smiles'}
	 
	# Call converter
    converter = LD50UnitConverter()

    # Load data
    train_x, test_x, train_y, test_y, train_epa, test_epa = load_data(task_type='classification')
    
	# Set seed and start dict with results
    gcn_results = dict()
    
    def generate_additional_seeds(master_seed):
        # Set the initial random seed using the master seed
        random.seed(master_seed)
        
        # Generate three additional seeds based on the master seed
        seed1 = random.randint(1, 1000000)
        seed2 = random.randint(1, 1000000)
        seed3 = random.randint(1, 1000000)
        
        # Return the three new seeds
        return seed1, seed2, seed3
    
    seed_list = generate_additional_seeds(630)

    # Three GCNs with randomly initialized weights
    i = 1
    for nseed in seed_list:
		
        model = benchmark['model']()
        model.seed = nseed
        model.epochs = 25
        model.learning_rate = 0.001
        model.emb_dim = 100

        initialize_weights(model, seed=nseed)

        train_y = train_y.flatten()
        test_y = test_y.flatten()

        # For loss curve
        model.val_data = (test_x, test_y)
        
        # Fit
        model.fit(train_x, train_y)
        
        fn = 'gcn_classification_gcn' + str(i) + '_lr0001_dim100'
        model.save_weights('../../data/replication_gcn/final_model_test/chkpts/{}.chkpt'.format(fn))
        
        # Predict
        y_hat = model.predict(test_x)
        
        results = pd.DataFrame({
            'smiles': test_x.flatten(),
            'prediction_epa': y_hat.flatten(),
            'actual_epa': test_y,
        })

        results.to_csv('../../data/replication_gcn/final_model_test/benchmark-models/{}_predictions_test.csv'.format(fn))
        
        pd.DataFrame(model.train_loss).to_csv('../../data/replication_gcn/final_model_test/loss/{}_loss_train.csv'.format(fn))
        pd.DataFrame(model.val_loss).to_csv('../../data/replication_gcn/final_model_test/loss/{}_loss_val.csv'.format(fn))

        model.val_loss = []
        model.train_loss = []
        embs_array = model.graph_embs
        
        with open('../../data/replication_gcn/final_model_test/graph_embeddings/regression_gcn{}_graphs.pickle'.format(i), 'wb') as handle:
            pickle.dump(embs_array, handle, protocol=pickle.HIGHEST_PROTOCOL)
        i += 1