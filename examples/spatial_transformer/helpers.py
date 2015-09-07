from __future__ import division, absolute_import
from __future__ import print_function, unicode_literals

import itertools
import subprocess
import numpy as np
import sklearn.datasets
import sklearn.cross_validation
import sklearn.metrics
import theano
import theano.tensor as T
import treeano
import treeano.nodes as tn
import treeano.lasagne.nodes as tl
import canopy

from treeano.sandbox.nodes import spatial_transformer as st
from treeano.sandbox.nodes import batch_normalization as bn

fX = theano.config.floatX

BATCH_SIZE = 500
# use the one from lasagne:
# https://github.com/Lasagne/Recipes/blob/master/examples/spatial_transformer_network.ipynb
CLUTTERED_MNIST_PATH = ("https://s3.amazonaws.com/lasagne/recipes/datasets/"
                        "mnist_cluttered_60x60_6distortions.npz")


def load_data():
    # download data
    subprocess.call(["wget", "-N", CLUTTERED_MNIST_PATH])

    data = np.load("mnist_cluttered_60x60_6distortions.npz")
    X_train, X_valid, X_test = [data[n].reshape((-1, 1, 60, 60))
                                for n in ["x_train", "x_valid", "x_test"]]
    y_train, y_valid, y_test = [np.argmax(data[n], axis=-1).astype('int32')
                                for n in ["y_train", "y_valid", "y_test"]]
    in_train = {"x": X_train, "y": y_train}
    in_valid = {"x": X_valid, "y": y_valid}
    in_test = {"x": X_test, "y": y_test}
    print("Train samples:", X_train.shape)
    print("Validation samples:", X_valid.shape)
    print("Test samples:", X_test.shape)
    return in_train, in_valid, in_test


def load_network(update_scale_factor):
    localization_network = tn.HyperparameterNode(
        "loc",
        tn.SequentialNode(
            "loc_seq",
            [tl.MaxPool2DDNNNode("loc_pool1"),
             tl.Conv2DDNNNode("loc_conv1"),
             tl.MaxPool2DDNNNode("loc_pool2"),
             bn.NoScaleBatchNormalizationNode("loc_bn1"),
             tn.ReLUNode("loc_relu1"),
             tl.Conv2DDNNNode("loc_conv2"),
             bn.NoScaleBatchNormalizationNode("loc_bn2"),
             tn.ReLUNode("loc_relu2"),
             tn.DenseNode("loc_fc1", num_units=50),
             bn.NoScaleBatchNormalizationNode("loc_bn3"),
             tn.ReLUNode("loc_relu3"),
             tn.DenseNode("loc_fc2",
                          num_units=6,
                          inits=[treeano.inits.NormalWeightInit(std=0.001)])]),
        num_filters=20,
        filter_size=(5, 5),
        pool_size=(2, 2),
    )

    st_node = st.AffineSpatialTransformerNode(
        "st",
        localization_network,
        output_shape=(20, 20))

    model = tn.HyperparameterNode(
        "model",
        tn.SequentialNode(
            "seq",
            [tn.InputNode("x", shape=(None, 1, 60, 60)),
             # scaling the updates of the spatial transformer
             # seems to be very helpful, to allow the clasification
             # net to learn what to look for, before prematurely
             # looking
             tn.UpdateScaleNode(
                 "st_update_scale",
                 st_node,
                 update_scale_factor=update_scale_factor),
             tl.Conv2DNode("conv1"),
             tl.MaxPool2DNode("mp1"),
             bn.NoScaleBatchNormalizationNode("bn1"),
             tn.ReLUNode("relu1"),
             tl.Conv2DNode("conv2"),
             tl.MaxPool2DNode("mp2"),
             bn.NoScaleBatchNormalizationNode("bn2"),
             tn.ReLUNode("relu2"),
             tn.GaussianDropoutNode("do1"),
             tn.DenseNode("fc1"),
             bn.NoScaleBatchNormalizationNode("bn3"),
             tn.ReLUNode("relu3"),
             tn.DenseNode("fc2", num_units=10),
             tn.SoftmaxNode("pred"),
             ]),
        num_filters=32,
        filter_size=(3, 3),
        pool_size=(2, 2),
        num_units=256,
        dropout_probability=0.5,
        inits=[treeano.inits.HeUniformInit()],
        bn_update_moving_stats=True,
    )

    with_updates = tn.HyperparameterNode(
        "with_updates",
        tn.AdamNode(
            "adam",
            {"subtree": model,
             "cost": tn.TotalCostNode("cost", {
                 "pred": tn.ReferenceNode("pred_ref", reference="model"),
                 "target": tn.InputNode("y", shape=(None,), dtype="int32")},
             )}),
        cost_function=treeano.utils.categorical_crossentropy_i32,
        learning_rate=2e-3,
    )
    network = with_updates.network()
    network.build()  # build eagerly to share weights
    return network


def train_network(network, in_train, in_valid, max_iters):
    valid_fn = canopy.handled_fn(
        network,
        [canopy.handlers.time_call(key="valid_time"),
         canopy.handlers.override_hyperparameters(deterministic=True),
         canopy.handlers.chunk_variables(batch_size=BATCH_SIZE,
                                         variables=["x", "y"])],
        {"x": "x", "y": "y"},
        {"valid_cost": "cost", "pred": "pred"})

    def validate(in_dict, result_dict):
        valid_out = valid_fn(in_valid)
        probabilities = valid_out.pop("pred")
        predicted_classes = np.argmax(probabilities, axis=1)
        result_dict["valid_accuracy"] = sklearn.metrics.accuracy_score(
            in_valid["y"], predicted_classes)
        result_dict.update(valid_out)

    train_fn = canopy.handled_fn(
        network,
        [canopy.handlers.time_call(key="total_time"),
         canopy.handlers.call_after_every(1, validate),
         canopy.handlers.time_call(key="train_time"),
         canopy.handlers.chunk_variables(batch_size=BATCH_SIZE,
                                         variables=["x", "y"])],
        {"x": "x", "y": "y"},
        {"train_cost": "cost"},
        include_updates=True)

    def callback(results_dict):
        print("{_iter:3d}: "
              "train_cost: {train_cost:0.3f} "
              "valid_cost: {valid_cost:0.3f} "
              "valid_accuracy: {valid_accuracy:0.3f}".format(**results_dict))

    print("Starting training...")
    canopy.evaluate_until(fn=train_fn,
                          gen=itertools.repeat(in_train),
                          max_iters=max_iters,
                          callback=callback)


def test_fn(network):
    return canopy.handled_fn(
        network,
        [canopy.handlers.override_hyperparameters(deterministic=True),
         canopy.handlers.batch_pad(batch_size=BATCH_SIZE, keys=["x"]),
         canopy.handlers.chunk_variables(batch_size=BATCH_SIZE,
                                         variables=["x"])],
        {"x": "x"},
        {"transformed": "st"})
