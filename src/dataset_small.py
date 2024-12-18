import numpy as np
from pathlib import Path
import os

def load_dataset(encoding, fold, stratified, subset, path_prefix=''):

    base_path = os.getcwd() + path_prefix + '/../../data/preprocessed/{type}/fold{no}_'.format(
        type = 'random' if not stratified else 'stratified',
        no = fold
    )

    x_paths = base_path + '{encoding}' + '_{set}{subset}.npz'
    y_paths = base_path + '{set}{subset}.npz'

    subset = ('_' + subset) if subset is not None else ''

    y_train = y_paths.format(set = 'y_train', subset = subset)
    smiles_train = y_paths.format(set = 'smiles_train', subset = subset)

    x_train = x_paths.format(encoding=encoding, set = 'x_train', subset = subset)

    if encoding == 'smiles':
        x_train = smiles_train

    x_train_load = np.load(x_train, allow_pickle=True)
    x_train_load = [x_train_load[f] for f in x_train_load.files]

    if len(x_train_load) == 1:
        x_train_load = x_train_load[0]

    # Slice first 10 rows of training data  GRITTA ADDED TO REDUCE SIZE OF DATASET TESTING ONLY
    x_train_load = x_train_load[:10]

    y_train_load = np.load(y_train, allow_pickle=True)['y'][:10]
    smiles_train_load = np.load(smiles_train, allow_pickle=True)['smiles'][:10]

    #TILL HERE CODE SEQUENCE
    y_test = y_paths.format(set = 'y_test', subset = subset)
    smiles_test = y_paths.format(set = 'smiles_test', subset = subset)

    x_test = x_paths.format(encoding=encoding, set = 'x_test', subset = subset)
    
    if encoding == 'smiles':
        x_test = smiles_test
        
    x_test_load = np.load(x_test, allow_pickle=True)
    x_test_load = [x_test_load[f] for f in x_test_load.files]

    if len(x_test_load) == 1:
        x_test_load = x_test_load[0]

    # Slice first 10 rows of test data FROM HERE ADDED
    x_test_load = x_test_load[:10]
    y_test_load = np.load(y_test, allow_pickle=True)['y'][:10]
    smiles_test_load = np.load(smiles_test, allow_pickle=True)['smiles'][:10]

    train = (x_train_load, y_train_load, smiles_train_load)
    test = (x_test_load, y_test_load, smiles_test_load)

    ### GRITTA ADDED THIS CODE SEQUENCE AND COMMENTED BELOW 2 LINES

    # train = (x_train_load, np.load(y_train, allow_pickle=True)['y'], np.load(smiles_train, allow_pickle=True)['smiles'])
    # test = (x_test_load, np.load(y_test, allow_pickle=True)['y'], np.load(smiles_test, allow_pickle=True)['smiles'])

    return (train, test)