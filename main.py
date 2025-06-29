import os
import json
import pickle
import random
import numpy as np
import tensorflow as tf

from pathlib import Path

from data_modules.data_utils import *
from data_modules.dataloader import DataLoader

from model.vae import *
from model.train import *
from model.encoder import *
from model.model_utils import *

from utils import *

import warnings

warnings.filterwarnings("ignore")

# SEED = 1
# os.environ['PYTHONHASHSEED'] = str(SEED)
# random.seed(SEED)
# np.random.seed(SEED)
# tf.random.set_seed(SEED)
# tf.keras.utils.set_random_seed(SEED)
# tf.config.experimental.enable_op_determinism()


os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"


def run(config):
    path_bert = Path("./artifacts/{}/bert/".format(config["dataset"]))
    path_vae = Path("./artifacts/{}/vae/".format(config["dataset"]))
    path_bert.mkdir(parents=True, exist_ok=True)
    path_vae.mkdir(parents=True, exist_ok=True)

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
        path=os.path.join("dataset", config["dataset"], "in_lbl_2_indx.txt"),
        intents=train_intents + test_intents + dev_intents,
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

    if config.get("use_balanced_sampling", False):
        print("Creating oversampled dataset for consistent BERT and VAE training...")

        # Create balanced data generator
        balanced_generator = BalancedDataGenerator(
            x=train_sentences,
            y=train_intents_encoded,
            tokenizer=tokenizer,
            max_length=max_length,
            batch_size=config["batch_size"],
            model_name=config["bert"],
            balance_method=config.get("sampling_method", "oversample"),
            random_state=config.get("random_state", 42),
        )
        stats = balanced_generator.get_oversampled_stats()
        print(f"Original dataset size: {stats['original_total']}")
        print(f"Oversampled dataset size: {stats['total_samples']}")
        print(f"Samples per class: {stats['samples_per_class']}")
        print(f"Original class distribution: {stats['original_class_counts']}")
        print(f"Oversampled class distribution: {stats['class_distribution']}")
        train_sentences, train_intents_encoded = (
            balanced_generator.get_oversampled_data()
        )
        indices = random.sample(
            range(len(train_sentences)), int(len(train_sentences) / 2)
        )
        train_sentences = [train_sentences[i] for i in indices]
        train_intents_encoded = [train_intents_encoded[i] for i in indices]

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

    train_tf = to_tf_format(
        (train_input_ids, train_attention_mask, train_token_type_ids),
        None,
        len(train_sentences),
        batch_size=16,
    )
    test_tf = to_tf_format(
        (test_input_ids, test_attention_mask, test_token_type_ids),
        None,
        len(test_sentences),
        batch_size=1,
    )
    dev_tf = to_tf_format(
        (dev_input_ids, dev_attention_mask, dev_token_type_ids),
        None,
        len(dev_sentences),
        batch_size=1,
    )
    ood_tf = to_tf_format(
        (ood_input_ids, ood_attention_mask, ood_token_type_ids),
        None,
        len(ood_sentences),
        batch_size=1,
    )
    print("Data preparation finished successfully!")

    print("------------------------------------------------------------------")

    print("Finetuning of bert is in progress...")
    # classifier = finetune(
    #     x_train=train_sentences + dev_sentences, y_train=np.concatenate((train_intents_encoded, dev_intents_encoded), axis=0),
    #     x_validation=test_sentences, y_validation=test_intents_encoded,
    #     max_length=max_length, num_labels=len(np.unique(np.array(train_intents))), path=os.path.join('artifacts', config['dataset'], 'bert/'),
    #     train=config['finetune'], first_layers_to_freeze=10, num_epochs=config['finetune_epochs'], model_name=config['bert']
    # )
    classifier = finetune(
        x_train=train_sentences + dev_sentences,
        y_train=np.concatenate((train_intents_encoded, dev_intents_encoded), axis=0),
        x_validation=test_sentences,
        y_validation=test_intents_encoded,
        max_length=max_length,
        num_labels=len(np.unique(np.array(train_intents))),
        path=os.path.join("artifacts", config["dataset"], "bert/"),
        train=config["finetune"],
        first_layers_to_freeze=10,
        num_epochs=config["finetune_epochs"],
        model_name=config["bert"],
        # New imbalanced dataset parameters from config
        use_class_weights=config.get("use_class_weights", False),
        class_weight_method=config.get("class_weight_method", "balanced"),
        use_focal_loss=config.get("use_focal_loss", False),
        focal_alpha=config.get("focal_alpha", 1.0),
        focal_gamma=config.get("focal_gamma", 2.0),
        use_balanced_sampling=False,
        sampling_method=config.get("sampling_method", "undersample"),
        use_warmup=config.get("use_warmup", False),
        warmup_steps=config.get("warmup_steps", None),
        warmup_initial_lr=config.get("warmup_initial_lr", 1e-6),
        warmup_strategy=config.get("warmup_strategy", "linear"),
        use_lr_schedule=config.get("use_lr_schedule", False),
        lr_schedule_type=config.get("lr_schedule_type", "exponential"),
        lr_decay_steps=config.get("lr_decay_steps", None),
        lr_decay_rate=config.get("lr_decay_rate", 0.96),
        random_state=config.get("random_state", 42),
    )
    classifier.load_weights(
        os.path.join("artifacts", config["dataset"], "bert/best_model")
    )
    bert.layers[0].set_weights(classifier.layers[0].get_weights())
    print(
        "Finetuning finished successfully and weights saved to {}".format(
            os.path.join("artifacts", config["dataset"], "bert/")
        )
    )

    print("------------------------------------------------------------------")

    print("VAE model creation is in progress...")
    # model = vae(
    #     bert=bert,
    #     encoder=encoder_model((config['vector_dim'],), config['latent_dim'], dims=config['encoder'], activation=config['activation']),
    #     decoder=decoder_model((config['latent_dim'],), dims=config['decoder'], activation=config['activation']),
    #     input_shape=((max_length,))
    # )

    model = vae(
        bert=bert,
        encoder=encoder_model(
            (config["vector_dim"],),
            config["latent_dim"],
            dims=config["encoder"],
            activation=config.get("activation", "relu"),  # Use relu instead of tanh
        ),
        decoder=decoder_model(
            (config["latent_dim"],),
            dims=config["decoder"],
            activation=config.get("activation", "relu"),
        ),
        input_shape=((max_length,)),
        beta=config.get("vae_beta", 1.0),  # Beta-VAE parameter
    )

    model.layers[3].trainable = False
    # Use a lower learning rate with optional decay
    initial_lr = config.get("vae_learning_rate", 0.0001)

    # For TF 2.8.2 compatibility, create optimizer differently based on whether we use schedule
    if config.get("use_lr_schedule", False):
        # Create learning rate schedule
        lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
            initial_learning_rate=initial_lr,
            decay_steps=1000,
            decay_rate=0.96,
            staircase=True,
        )
        optimizer = tf.keras.optimizers.Adam(learning_rate=lr_schedule)
    else:
        # Use constant learning rate
        optimizer = tf.keras.optimizers.Adam(learning_rate=initial_lr)

    train_loss_metric = tf.keras.metrics.Mean()
    val_loss_metric = tf.keras.metrics.Mean()
    print("Model is created successfully!")

    print("------------------------------------------------------------------")

    print("Training of VAE is in progress...")
    # train_loop(
    #     model,
    #     optimizer,
    #     train_tf,
    #     dev_tf,
    #     path=os.path.join('artifacts', config['dataset'], 'vae', 'vae.h5'),
    #     batch_size=config['batch_size'],
    #     num_epochs=config['train_epochs'],
    #     train_loss_metric=train_loss_metric, val_loss_metric=val_loss_metric
    # )
    history = train_loop_stable(
        model,
        optimizer,
        train_tf,
        dev_tf,
        path=os.path.join("artifacts", config["dataset"], "vae", "vae.h5"),
        batch_size=config["batch_size"],
        num_epochs=config["train_epochs"],
        train_loss_metric=train_loss_metric,
        val_loss_metric=val_loss_metric,
        early_stopping_patience=config.get("vae_early_stopping_patience", 10),
        clip_norm=config.get("vae_clip_norm", 1.0),
        lr_reduce_patience=config.get("vae_lr_reduce_patience", 5),
    )
    model.load_weights(os.path.join("artifacts", config["dataset"], "vae", "vae.h5"))
    with open(
        os.path.join("artifacts", config["dataset"], "vae", "training_history.pkl"),
        "wb",
    ) as f:
        pickle.dump(history, f)
    print(
        "Training is done and weights saved to {}".format(
            os.path.join("artifacts", config["dataset"], "vae", "vae.h5")
        )
    )

    print("Verifying model outputs...")
    sample_batch = next(iter(dev_tf))
    sample_output = model(sample_batch, training=False)
    print(f"Sample output shape: {sample_output.shape}")
    print(
        f"Sample output range: [{tf.reduce_min(sample_output):.3f}, {tf.reduce_max(sample_output):.3f}]"
    )

    if tf.reduce_any(tf.math.is_nan(sample_output)):
        print("WARNING: Model is producing NaN outputs!")
    else:
        print("Model outputs look valid.")

    print("------------------------------------------------------------------")

    print("Calculating train and dev loss for visualization...")
    train_tf = to_tf_format(
        (train_input_ids, train_attention_mask, train_token_type_ids),
        None,
        len(train_sentences),
        batch_size=1,
    )
    train_loss = compute_loss_stable(model, train_tf)
    dev_loss = compute_loss_stable(model, dev_tf)
    train_loss_normalized = normalize(
        train_loss, path=os.path.join("artifacts", config["dataset"]), mode="train"
    )
    dev_loss_normalized = normalize(
        dev_loss, path=os.path.join("artifacts", config["dataset"]), mode="eval"
    )
    visualize(
        train_loss_normalized,
        os.path.join(
            "artifacts",
            config["dataset"],
            "vae_loss_for_{}_train.png".format(config["dataset"]),
        ),
    )
    visualize(
        dev_loss_normalized,
        os.path.join(
            "artifacts",
            config["dataset"],
            "vae_loss_for_{}_dev.png".format(config["dataset"]),
        ),
    )
    print(
        "You can use figures in {} to decide what threshold should be used.".format(
            os.path.join("artifacts", config["dataset"])
        )
    )

    print("------------------------------------------------------------------")


if __name__ == "__main__":
    config_file = open("./config.json")
    config = json.load(config_file)

    run(config)
