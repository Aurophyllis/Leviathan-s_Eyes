"""Question-to-frame relevance using the pinned CLIP ONNX export."""

import json

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps
from tokenizers import Tokenizer


class ClipScorer:
    def __init__(self, folder, batch_size=8):
        if batch_size < 1:
            raise ValueError("CLIP batch size must be positive")
        self.batch_size = batch_size
        preprocess = json.loads((folder / "preprocessor_config.json").read_text(encoding="utf-8"))
        crop = preprocess["crop_size"]
        self.width, self.height = crop["width"], crop["height"]
        self.short = preprocess["size"]["shortest_edge"]
        self.mean = np.array(preprocess["image_mean"], dtype=np.float32)[:, None, None]
        self.std = np.array(preprocess["image_std"], dtype=np.float32)[:, None, None]
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=77)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(folder / "model_quantized.onnx"),
                                           sess_options=options, providers=["CPUExecutionProvider"])
        self.inputs = {item.name for item in self.session.get_inputs()}

    def prepare(self, path):
        with Image.open(path) as image:
            image = image.convert("RGB")
            width, height = image.size
            size = (round(width * self.short / min(width, height)),
                    round(height * self.short / min(width, height)))
            image = image.resize(size, Image.Resampling.BICUBIC)
            image = ImageOps.crop(image, (
                (image.width - self.width) // 2, (image.height - self.height) // 2,
                (image.width - self.width + 1) // 2, (image.height - self.height + 1) // 2,
            ))
            values = np.asarray(image, dtype=np.float32).transpose(2, 0, 1) / 255.0
        return (values - self.mean) / self.std

    def score(self, question, paths):
        encoded = self.tokenizer.encode(question)
        tokens = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
        }
        feed = {name: value for name, value in tokens.items() if name in self.inputs}
        result = []
        for start in range(0, len(paths), self.batch_size):
            feed["pixel_values"] = np.stack([self.prepare(path) for path in paths[start:start + self.batch_size]])
            text, images = self.session.run(["text_embeds", "image_embeds"], feed)
            text = text[0] / max(float(np.linalg.norm(text[0])), 1e-9)
            images = images / np.maximum(np.linalg.norm(images, axis=1, keepdims=True), 1e-9)
            result.extend(((np.clip(images @ text, -1, 1) + 1) / 2).astype(float).tolist())
        return result
