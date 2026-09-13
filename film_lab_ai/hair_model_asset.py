"""Pinned asset identity shared by offline inference and build preparation."""

MODEL_FILE = "selfie_multiclass_256x256.tflite"
MODEL_BYTES = 16371837
MODEL_SHA256 = "c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/image_segmenter/"
             "selfie_multiclass_256x256/float32/1/" + MODEL_FILE)
MODEL_ID = "selfie-multiclass-v1-" + MODEL_SHA256[:12]
MODEL_CARD = "https://storage.googleapis.com/mediapipe-assets/Model%20Card%20Multiclass%20Segmentation.pdf"
LICENSE_FILE = "HairSegmentation-APACHE-2.0.txt"
LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
