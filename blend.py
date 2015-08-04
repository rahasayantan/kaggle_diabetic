"""Blend features extracted with Conv Nets and make predictions/submissions."""
from __future__ import division, print_function
from datetime import datetime
from glob import glob

import click
import numpy as np
import pandas as pd
import theano
from lasagne import init
from lasagne.updates import adam
from lasagne.nonlinearities import rectify
from lasagne.layers import DenseLayer, InputLayer, FeaturePoolLayer
from nolearn.lasagne import BatchIterator
from sklearn.metrics import confusion_matrix
from sklearn.preprocessing import StandardScaler

import data
import nn
import util

np.random.seed(9)

START_LR = 0.0005
END_LR = START_LR * 0.001
L1 = 2e-5
L2 = 0.005
N_ITER = 100
PATIENCE = 20
POWER = 0.5
N_HIDDEN_1 = 32
N_HIDDEN_2 = 32
BATCH_SIZE = 128

SCHEDULE = {
    60: START_LR / 10.0,
    80: START_LR / 100.0,
    90: START_LR / 1000.0,
    N_ITER: 'stop'
}


class BlendNet(nn.Net):

    def set_split(self, files, labels):
        """Override train/test split method to use our default split."""
        def split(X, y, eval_size):
            if eval_size:
                tr, te = data.split_indices(files, labels, eval_size)
                return X[tr], X[te], y[tr], y[te]
            else:
                return X, X[len(X):], y, y[len(y):]
        setattr(self, 'train_test_split', split)


class ResampleIterator(BatchIterator):

    def __init__(self, batch_size, resample_prob=0.2, shuffle_prob=0.5):
        self.resample_prob = resample_prob
        self.shuffle_prob = shuffle_prob
        super(ResampleIterator, self).__init__(batch_size)

    def __iter__(self):
        n_samples = self.X.shape[0]
        bs = self.batch_size
        indices = data.balance_per_class_indices(self.y.ravel())
        for i in range((n_samples + bs - 1) // bs):
            r = np.random.rand()
            if r < self.resample_prob:
                sl = indices[np.random.randint(0, n_samples, size=bs)]
            elif r < self.shuffle_prob:
                sl = np.random.randint(0, n_samples, size=bs)
            else:
                sl = slice(i * bs, (i + 1) * bs)
            Xb = self.X[sl]
            if self.y is not None:
                yb = self.y[sl]
            else:
                yb = None
            yield self.transform(Xb, yb)


def get_estimator(n_features, files, labels, eval_size=0.1):
    layers = [
        (InputLayer, {'shape': (None, n_features)}),
        (DenseLayer, {'num_units': N_HIDDEN_1, 'nonlinearity': rectify,
                      'W': init.Orthogonal('relu'),
                      'b': init.Constant(0.01)}),
        (FeaturePoolLayer, {'pool_size': 2}),
        (DenseLayer, {'num_units': N_HIDDEN_2, 'nonlinearity': rectify,
                      'W': init.Orthogonal('relu'),
                      'b': init.Constant(0.01)}),
        (FeaturePoolLayer, {'pool_size': 2}),
        (DenseLayer, {'num_units': 1, 'nonlinearity': None}),
    ]
    args = dict(
        update=adam,
        update_learning_rate=theano.shared(util.float32(START_LR)),
        batch_iterator_train=ResampleIterator(BATCH_SIZE),
        batch_iterator_test=BatchIterator(BATCH_SIZE),
        objective=nn.get_objective(l1=L1, l2=L2),
        eval_size=eval_size,
        custom_score=('kappa', util.kappa) if eval_size > 0.0 else None,
        on_epoch_finished=[
            nn.Schedule('update_learning_rate', SCHEDULE),
        ],
        regression=True,
        max_epochs=N_ITER,
        verbose=1,
    )
    net = BlendNet(layers, **args)
    net.set_split(files, labels)
    return net


@click.command()
@click.option('--cnf', default='configs/c_128_4x4_32.py', show_default=True,
              help="Path or name of configuration module.")
@click.option('--predict', is_flag=True, default=False, show_default=True,
              help="Make predictions on test set features after training.")
@click.option('--per_patient', is_flag=True, default=False, show_default=True,
              help="Blend features of both patient eyes.")
@click.option('--features_file', default=None, show_default=True,
              help="Read features from specified file.")
@click.option('--directory', default=data.FEATURE_DIR, show_default=True,
              help="Blend once for each (sub)directory and file in directory")
@click.option('--n_iter', default=1, show_default=True,
              help="Number of times to fit and average.")
def fit(cnf, predict, per_patient, features_file, n_iter, directory):

    config = util.load_module(cnf).config
    image_files = data.get_image_files(config.get('train_dir'))
    names = data.get_names(image_files)
    labels = data.get_labels(names).astype(np.float32)[:, np.newaxis]

    feat_dirs = glob('{}/*/'.format(directory))
    feat_files = glob('{}/*.*'.format(directory))

    if features_file is None:
        X_trains = [data.load_features(directory=directory)
                    for directory in feat_dirs] \
            + [data.load_features(features_file=features_file)
               for features_file in feat_files]
    else:
        X_trains = [data.load_features(features_file=features_file)]

    scalers = [StandardScaler() for _ in X_trains]
    X_trains = [scaler.fit_transform(X_train)
                for scaler, X_train in zip(scalers, X_trains)]

    if predict:

        if features_file is None:
            X_tests = [data.load_features(directory=directory, test=True)
                       for directory in feat_dirs]
        else:
            features_file = features_file.replace('train', 'test')
            X_tests = [data.load_features(test=True,
                                           features_file=features_file)]

        X_tests = [scaler.transform(X_test)
                   for scaler, X_test in zip(scalers, X_tests)]

    tr, te = data.split_indices(image_files, labels)

    if not predict:

        print("feature matrix shape {}".format(X_train.shape))

        y_preds = []
        for i in range(n_iter):
            print("iteration {} / {}".format(i + 1, n_iter))
            for X_train in X_trains:
                print("fitting split training set")
                X = data.per_patient_reshape(X_train) \
                    if per_patient else X_train
                est = get_estimator(X.shape[1], image_files, labels)
                est.fit(X, labels)

                y_pred = est.predict(X[te]).ravel()
                y_preds.append(y_pred)
                y_pred = np.mean(y_preds, axis=0)
                y_pred = np.clip(np.round(y_pred).astype(int),
                                 np.min(labels), np.max(labels))

                print("kappa at iteration ", i, util.kappa(labels[te], y_pred))
                print("confusion matrix")
                print(confusion_matrix(labels[te], y_pred))

    if predict:

        y_preds = []
        for i in range(n_iter):
            print("iteration {} / {}".format(i + 1, n_iter))
            for X_train, X_test in zip(X_trains, X_tests):
                print("fitting full training set")
                X = per_patient_reshape(X_train) if per_patient else X_train
                Xt = data.per_patient_reshape(X_test) \
                    if per_patient else X_test
                est = get_estimator(X.shape[1], image_files, labels, 
                                    eval_size=0.0)
                est.fit(X, labels)
                y_pred = est.predict(Xt).ravel()
                y_preds.append(y_pred)

        y_pred = np.mean(y_preds, axis=0)
        y_pred = np.clip(np.round(y_pred),
                         np.min(labels), np.max(labels)).astype(int)

        submission_filename = util.get_submission_filename()
        image_files = data.get_image_files(config.get('test_dir'))
        names = data.get_names(image_files)
        image_column = pd.Series(names, name='image')
        level_column = pd.Series(y_pred, name='level')
        predictions = pd.concat([image_column, level_column], axis=1)

        print("tail of predictions file")
        print(predictions.tail())

        predictions.to_csv(submission_filename, index=False)
        print("saved predictions to {}".format(submission_filename))


if __name__ == '__main__':
    fit()
