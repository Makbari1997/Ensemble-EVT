import os
import json
import pickle

import numpy as np
import tensorflow as tf

from scipy import stats
from sklearn import metrics
from sklearn.metrics import roc_auc_score


from data_modules.data_utils import *
from data_modules.dataloader import DataLoader

from model.vae import *
from model.encoder import *
from model.model_utils import *

# from model.train import compute_loss_safe

from utils import *


def compute_loss_safe(model, data):
    """
    Compute loss with NaN/Inf checking and handling
    """
    losses = []
    skipped_count = 0

    for step, (x, y, z) in enumerate(data):
        try:
            # Compute loss
            logits = model([x, y, z], training=False)
            loss_value = model.losses

            if isinstance(loss_value, list):
                loss_value = loss_value[0]

            loss_numpy = loss_value.numpy()

            # Check for NaN or Inf
            if np.isnan(loss_numpy) or np.isinf(loss_numpy):
                print(f"Warning: NaN/Inf loss detected at step {step}, skipping")
                skipped_count += 1
                continue

            losses.append(loss_numpy)

        except Exception as e:
            print(f"Error computing loss at step {step}: {e}")
            skipped_count += 1
            continue

    if len(losses) == 0:
        print("ERROR: All losses were invalid!")
        # Return a default high loss value instead of empty array
        return np.array([10.0])  # High loss indicates anomaly

    if skipped_count > 0:
        print(f"Skipped {skipped_count} samples due to invalid losses")

    return np.array(losses)


def fit_evt_models(
    classifier, tokenizer, losses, sentences, true_classes, max_length, fpr=0.05
):
    """
    Fit EVT models for each class based on reconstruction losses and classifier probabilities

    Args:
        classifier: The fine-tuned BERT classifier
        tokenizer: BERT tokenizer
        losses: List of reconstruction losses for in-domain validation data
        sentences: List of in-domain validation sentences
        true_classes: List of true class indices for in-domain validation data
        max_length: Maximum sequence length
        fpr: Desired false positive rate

    Returns:
        Dictionary mapping class IDs to thresholds
    """
    # Group validation samples by class
    class_to_samples = {}

    for i, (loss, sen, cls) in enumerate(zip(losses, sentences, true_classes)):
        # Get classifier output
        inputs = __predict_preprocess__(sen, tokenizer, max_length)
        logits = classifier.predict(inputs)[0]
        probs = tf.nn.softmax(logits, axis=1).numpy()[0]

        # Get maximum probability
        max_prob = np.max(probs)

        # Calculate ensemble score with alpha=0.5 (will optimize later)
        ood_score = 0.5 * (1 - max_prob) + 0.5 * loss

        if cls not in class_to_samples:
            class_to_samples[cls] = []

        class_to_samples[cls].append(ood_score)

    # Fit EVT models for each class
    evt_models = {}
    thresholds = {}

    for cls, scores in class_to_samples.items():
        # Fit GEV distribution
        scores_array = np.array(scores)
        shape, loc, scale = stats.genextreme.fit(-scores_array)
        evt_models[cls] = (shape, loc, scale)

        # Calculate threshold based on desired FPR
        threshold = -stats.genextreme.ppf(1 - fpr, shape, loc, scale)
        thresholds[cls] = threshold

    return thresholds, evt_models


def fit_evt_models_robust(
    classifier,
    tokenizer,
    losses,
    sentences,
    true_classes,
    max_length,
    fpr=0.05,
    contamination_ratio=0.05,
):
    """
    Fit EVT models with outlier detection and robustness improvements
    """
    # Group validation samples by class
    class_to_samples = {}

    for i, (loss, sen, cls) in enumerate(zip(losses, sentences, true_classes)):
        # Skip invalid losses
        if np.isnan(loss) or np.isinf(loss):
            continue

        # Get classifier output
        inputs = __predict_preprocess__(sen, tokenizer, max_length)
        logits = classifier.predict(inputs)[0]
        probs = tf.nn.softmax(logits, axis=1).numpy()[0]

        # Get maximum probability
        max_prob = np.max(probs)

        # Calculate ensemble score with alpha=0.5
        ood_score = 0.5 * (1 - max_prob) + 0.5 * loss

        # Skip invalid scores
        if np.isnan(ood_score) or np.isinf(ood_score):
            continue

        if cls not in class_to_samples:
            class_to_samples[cls] = []

        class_to_samples[cls].append(ood_score)

    # Fit EVT models for each class with outlier removal
    evt_models = {}
    thresholds = {}

    for cls, scores in class_to_samples.items():
        if len(scores) < 10:  # Need minimum samples
            print(
                f"Warning: Class {cls} has only {len(scores)} samples, using percentile threshold"
            )
            thresholds[cls] = np.percentile(scores, 95)
            continue

        scores_array = np.array(scores)

        # Remove outliers using IQR method
        q1 = np.percentile(scores_array, 25)
        q3 = np.percentile(scores_array, 75)
        iqr = q3 - q1
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr

        # Keep only scores within bounds
        scores_clean = scores_array[
            (scores_array >= lower_bound) & (scores_array <= upper_bound)
        ]

        if len(scores_clean) < 5:
            print(f"Warning: Too few samples after outlier removal for class {cls}")
            thresholds[cls] = np.percentile(scores_array, 95)
            continue

        try:
            # Fit GEV distribution on clean data
            shape, loc, scale = stats.genextreme.fit(-scores_clean)

            # Check if parameters are reasonable
            if abs(shape) > 2 or scale <= 0 or scale > 10:
                print(
                    f"Warning: Unreasonable EVT parameters for class {cls}, using percentile"
                )
                thresholds[cls] = np.percentile(scores_clean, 100 * (1 - fpr))
            else:
                evt_models[cls] = (shape, loc, scale)
                # Calculate threshold based on desired FPR
                threshold = -stats.genextreme.ppf(1 - fpr, shape, loc, scale)

                # Sanity check threshold
                if (
                    threshold < np.min(scores_clean)
                    or threshold > np.max(scores_array) * 2
                ):
                    print(
                        f"Warning: EVT threshold out of range for class {cls}, using percentile"
                    )
                    threshold = np.percentile(scores_clean, 100 * (1 - fpr))

                thresholds[cls] = threshold

        except Exception as e:
            print(f"Error fitting EVT for class {cls}: {e}, using percentile")
            thresholds[cls] = np.percentile(scores_clean, 100 * (1 - fpr))

    return thresholds, evt_models


def __predict_preprocess__(x, tokenizer, max_length):
    x_tokenized = tokenizer(
        x,
        return_tensors="tf",
        padding="max_length",
        max_length=max_length,
        truncation=True,
    )
    return {i: x_tokenized[i] for i in tokenizer.model_input_names}


def predict(
    classifier: object,
    tokenizer: object,
    losses: list,
    sentences: list,
    threshold: float,
    ood_label: int,
    max_length: int,
) -> list:
    labels = []
    for loss, sen in zip(losses, sentences):
        if loss <= threshold:
            labels.append(
                np.argmax(
                    classifier.predict(
                        __predict_preprocess__(sen, tokenizer, max_length)
                    )[0],
                    axis=1,
                )[0]
            )
        else:
            labels.append(ood_label)
    return labels


def run(config):
    print("Loading data from {}...".format(os.path.join("data", config["dataset"])))
    dataloader = DataLoader(path=os.path.join("dataset", config["dataset"]))
    train_sentences, train_intents = dataloader.train_loader()
    dev_sentences, dev_intents = dataloader.dev_loader()
    test_sentences, test_intents = dataloader.test_loader()
    ood_sentences, ood_intents = dataloader.ood_loader()
    print("Data is loaded successfully!")

    print("------------------------------------------------------------------")

    print("Encoding intent labels...")
    in_lbl_2_indx = get_lbl_2_indx(
        path=os.path.join("dataset", config["dataset"], "in_lbl_2_indx.txt")
    )

    train_intents_encoded = one_hot_encoder(train_intents, in_lbl_2_indx)
    test_intents_encoded = one_hot_encoder(test_intents, in_lbl_2_indx)
    dev_intents_encoded = one_hot_encoder(dev_intents, in_lbl_2_indx)

    ood_lbl_2_indx = get_lbl_2_indx(
        path=os.path.join("dataset", config["dataset"], "ood_lbl_2_indx.txt"),
        intents=ood_intents,
    )
    ood_intents_encoded = one_hot_encoder(ood_intents, ood_lbl_2_indx)
    print("Encoding done successfully!")

    print("------------------------------------------------------------------")

    max_length = max_sentence_length(train_sentences, policy=config["seq_length"])

    print("Downloading {}".format(config["bert"]))
    bert, tokenizer = get_bert(config["bert"])
    print("Download finished successfully!")

    print("------------------------------------------------------------------")

    print("Preparing data for bert, it may take a few minutes...")
    train_input_ids, train_attention_mask, train_token_type_ids = preprocessing(
        tokenizer, train_sentences, max_length
    )
    test_input_ids, test_attention_mask, test_token_type_ids = preprocessing(
        tokenizer, test_sentences, max_length
    )
    dev_input_ids, dev_attention_mask, dev_token_type_ids = preprocessing(
        tokenizer, dev_sentences, max_length
    )
    ood_input_ids, ood_attention_mask, ood_token_type_ids = preprocessing(
        tokenizer, ood_sentences, max_length
    )

    # train_tf = to_tf_format((train_input_ids, train_attention_mask, train_token_type_ids), None, len(train_sentences), batch_size=1)
    test_tf = to_tf_format(
        (test_input_ids, test_attention_mask, test_token_type_ids),
        None,
        len(test_sentences),
        batch_size=1,
    )
    # dev_tf = to_tf_format((dev_input_ids, dev_attention_mask, dev_token_type_ids), None, len(dev_sentences), batch_size=1)
    ood_tf = to_tf_format(
        (ood_input_ids, ood_attention_mask, ood_token_type_ids),
        None,
        len(ood_sentences),
        batch_size=1,
    )
    print("Data preparation finished successfully!")

    print("------------------------------------------------------------------")

    print(
        "Loading bert weights from {}".format(
            os.path.join("artifacts", config["dataset"], "bert/")
        )
    )
    classifier = finetune(
        x_train=train_sentences + dev_sentences,
        y_train=np.concatenate((train_intents_encoded, dev_intents_encoded), axis=0),
        x_validation=test_sentences,
        y_validation=test_intents_encoded,
        max_length=max_length,
        num_labels=len(np.unique(np.array(train_intents))),
        path=os.path.join("artifacts", config["dataset"], "bert/"),
        train=False,
        first_layers_to_freeze=11,
        num_epochs=config["finetune_epochs"],
        model_name=config["bert"],
    )
    classifier.load_weights(
        os.path.join("artifacts", config["dataset"], "bert/best_model")
    )
    bert.layers[0].set_weights(classifier.layers[0].get_weights())
    print("------------------------------------------------------------------")

    print("VAE model creation is in progress...")
    model = vae(
        bert=bert,
        encoder=encoder_model(
            (config["vector_dim"],),
            config["latent_dim"],
            dims=config["encoder"],
            activation=config["activation"],
        ),
        decoder=decoder_model(
            (config["latent_dim"],),
            dims=config["decoder"],
            activation=config["activation"],
        ),
        input_shape=((max_length,)),
    )

    model.layers[3].trainable = False
    # optimizer = tf.keras.optimizers.Adam(learning_rate=config['vae_learning_rate'])
    # train_loss_metric = tf.keras.metrics.Mean()
    # val_loss_metric = tf.keras.metrics.Mean()

    model.load_weights(os.path.join("artifacts", config["dataset"], "vae", "vae.h5"))
    print(
        "Model was created successfully and weights were loaded from {}.".format(
            os.path.join("artifacts", config["dataset"], "vae", "vae.h5")
        )
    )

    print("------------------------------------------------------------------")

    # Calculate losses for dev, test, and ood sets
    train_tf = to_tf_format(
        (train_input_ids, train_attention_mask, train_token_type_ids),
        None,
        len(train_sentences),
        batch_size=1,
    )
    dev_tf = to_tf_format(
        (dev_input_ids, dev_attention_mask, dev_token_type_ids),
        None,
        len(dev_sentences),
        batch_size=1,
    )
    test_tf = to_tf_format(
        (test_input_ids, test_attention_mask, test_token_type_ids),
        None,
        len(test_sentences),
        batch_size=1,
    )
    ood_tf = to_tf_format(
        (ood_input_ids, ood_attention_mask, ood_token_type_ids),
        None,
        len(ood_sentences),
        batch_size=1,
    )

    train_loss = compute_loss_safe(model, train_tf)
    dev_loss = compute_loss_safe(model, dev_tf)
    test_loss = compute_loss_safe(model, test_tf)
    ood_loss = compute_loss_safe(model, ood_tf)

    # Fix normalization - use proper function
    normalized_train_loss = normalize(
        train_loss, path=os.path.join("artifacts", config["dataset"]), mode="train"
    )
    normalized_dev_loss = normalize(
        dev_loss, path=os.path.join("artifacts", config["dataset"]), mode="eval"
    )
    normalized_test_loss = normalize(
        test_loss, path=os.path.join("artifacts", config["dataset"]), mode="eval"
    )
    normalized_ood_loss = normalize(
        ood_loss, path=os.path.join("artifacts", config["dataset"]), mode="eval"
    )
    print(test_loss)
    # Visualize test and OOD losses
    visualize(
        normalized_test_loss,
        os.path.join(
            "artifacts",
            config["dataset"],
            "vae_loss_for_{}_test.png".format(config["dataset"]),
        ),
    )
    visualize(
        normalized_ood_loss,
        os.path.join(
            "artifacts",
            config["dataset"],
            "vae_loss_for_{}_ood.png".format(config["dataset"]),
        ),
    )

    # Choose detection approach based on configuration
    if config.get("use_evt_vae", False):
        # EVT-VAE Only approach
        print("------------------------------------------------------------------")
        print("Using EVT for VAE losses only")

        # Apply EVT to VAE losses
        evt_results = evt_vae_only(
            normalized_dev_loss,
            # dev_loss,
            normalized_test_loss,
            # test_loss,
            normalized_ood_loss,
            # ood_loss,
            desired_fpr=config.get("evt_fpr", 0.05),
            tail_fraction=config.get("tail_fraction", 0.2),
            min_tail_size=config.get("min_tail_size", 30),
        )

        # Save EVT results
        evt_path = os.path.join("artifacts", config["dataset"], "evt_vae")
        os.makedirs(evt_path, exist_ok=True)

        with open(os.path.join(evt_path, "evt_results.pkl"), "wb") as f:
            pickle.dump(evt_results, f)

        # Visualize VAE losses with EVT threshold
        visualize_vae_losses(
            normalized_test_loss,
            normalized_ood_loss,
            evt_results["evt"]["threshold"],
            os.path.join(evt_path, "vae_losses_evt_threshold.png"),
            title="VAE Loss Distribution with EVT Threshold",
        )

        # Use the EVT threshold for final prediction
        evt_threshold = evt_results["evt"]["threshold"]
        print(f"Using EVT threshold: {evt_threshold:.4f}")

        # Evaluate on test and OOD data
        eval_losses = np.concatenate([normalized_test_loss, normalized_ood_loss])
        # eval_losses = np.concatenate([test_loss, ood_loss])
        eval_sentences = test_sentences + ood_sentences

        # Make predictions using VAE loss threshold
        y_pred_multiclass = predict(
            classifier,
            tokenizer,
            eval_losses,
            eval_sentences,
            evt_threshold,
            len(in_lbl_2_indx),
            max_length,
        )
        y_true_multiclass = [in_lbl_2_indx[i] for i in test_intents] + [
            len(in_lbl_2_indx)
        ] * len(ood_sentences)

        # For binary classification (in-domain vs OOD)
        y_true_binary = [0] * len(test_sentences) + [1] * len(ood_sentences)
        y_pred_binary = [0 if loss <= evt_threshold else 1 for loss in eval_losses]

        # Calculate metrics
        print("----------------------------------")
        print(
            f'Multi class macro f1: {metrics.f1_score(y_true_multiclass, y_pred_multiclass, average="macro"):.4f}'
        )
        print(
            f'Multi class micro f1: {metrics.f1_score(y_true_multiclass, y_pred_multiclass, average="micro"):.4f}'
        )
        print("\n")
        print(
            f'Binary class macro f1: {metrics.f1_score(y_true_binary, y_pred_binary, average="macro"):.4f}'
        )
        print(
            f'Binary class micro f1: {metrics.f1_score(y_true_binary, y_pred_binary, average="micro"):.4f}'
        )

        try:
            auc_roc = metrics.roc_auc_score(y_true_binary, eval_losses)
            print(f"AUC-ROC: {auc_roc:.4f}")
        except:
            print("Could not calculate AUC-ROC")
    else:
        # Simple threshold approach
        print("------------------------------------------------------------------")
        print("Using fixed threshold approach")

        # Use fixed threshold from config
        fixed_threshold = config.get("fixed_threshold", 0.2)
        print(f"Using fixed threshold: {fixed_threshold:.4f}")

        # Visualize VAE losses with fixed threshold
        plt.figure(figsize=(10, 6))
        plt.hist(
            normalized_test_loss, bins=30, alpha=0.7, label="In-domain", color="blue"
        )
        plt.hist(normalized_ood_loss, bins=30, alpha=0.7, label="OOD", color="red")
        plt.axvline(
            fixed_threshold,
            color="black",
            linestyle="--",
            label=f"Threshold: {fixed_threshold:.4f}",
        )
        plt.xlabel("Normalized VAE Loss")
        plt.ylabel("Count")
        plt.title("VAE Loss Distribution with Fixed Threshold")
        plt.legend()
        plt.savefig(
            os.path.join(
                "artifacts", config["dataset"], "vae_losses_fixed_threshold.png"
            )
        )

        # Combine test and OOD data
        eval_loss = np.concatenate([normalized_test_loss, normalized_ood_loss])
        eval_sentences = test_sentences + ood_sentences

        # Make predictions using fixed threshold
        y_pred_multiclass = predict(
            classifier,
            tokenizer,
            eval_loss,
            eval_sentences,
            fixed_threshold,
            len(in_lbl_2_indx),
            max_length,
        )
        y_true_multiclass = [in_lbl_2_indx[i] for i in test_intents] + [
            len(in_lbl_2_indx)
        ] * len(ood_sentences)

        # For binary classification (in-domain vs OOD)
        y_true_binary = [0] * len(test_sentences) + [1] * len(ood_sentences)
        y_pred_binary = [0 if loss <= fixed_threshold else 1 for loss in eval_loss]

        # Calculate metrics
        print("----------------------------------")
        print(
            f'Multi class macro f1: {metrics.f1_score(y_true_multiclass, y_pred_multiclass, average="macro"):.4f}'
        )
        print(
            f'Multi class micro f1: {metrics.f1_score(y_true_multiclass, y_pred_multiclass, average="micro"):.4f}'
        )
        print("\n")
        print(
            f'Binary class macro f1: {metrics.f1_score(y_true_binary, y_pred_binary, average="macro"):.4f}'
        )
        print(
            f'Binary class micro f1: {metrics.f1_score(y_true_binary, y_pred_binary, average="micro"):.4f}'
        )

        try:
            auc_roc = metrics.roc_auc_score(y_true_binary, eval_loss)
            print(f"AUC-ROC: {auc_roc:.4f}")
        except:
            print("Could not calculate AUC-ROC")

    print("------------------------------------------------------------------")


if __name__ == "__main__":
    config_file = open("./config.json")
    config = json.load(config_file)

    run(config)
