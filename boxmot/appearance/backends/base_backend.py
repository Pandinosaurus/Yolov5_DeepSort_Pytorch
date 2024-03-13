import cv2
import torch
import gdown
import numpy as np
from abc import ABC, abstractmethod
from boxmot.appearance.backbones import build_model, get_nr_classes
from boxmot.appearance.reid_model_factory import (
    get_model_name,
    get_model_url
)

class BaseModelBackend:
    def __init__(self, weights, device, half):
        self.weights = self.weights[0] if isinstance(self.weights, list) else self.weights
        self.device = device
        self.half = half
        self.model = None
        self.cuda = torch.cuda.is_available() and self.device.type != "cpu"
        
        self.download_model(self.weights)
        self.model_name = get_model_name(self.weights)

        self.model = build_model(
            self.model_name,
            num_classes=get_nr_classes(self.weights),
            pretrained=not (self.weights and self.weights.is_file()),
            use_gpu=device,
        )
        self.load_model(self.weights)

        
    @abstractmethod
    def load_model(self):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def get_crops(self, xyxys, img):
        crops = []
        h, w = img.shape[:2]
        resize_dims = (128, 256)
        interpolation_method = cv2.INTER_LINEAR
        mean_array = np.array([0.485, 0.456, 0.406])
        std_array = np.array([0.229, 0.224, 0.225])
        # dets are of different sizes so batch preprocessing is not possible
        for box in xyxys:
            x1, y1, x2, y2 = box.astype('int')
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(w - 1, x2)
            y2 = min(h - 1, y2)
            crop = img[y1:y2, x1:x2]
            # resize
            crop = cv2.resize(
                crop,
                resize_dims,  # from (x, y) to (128, 256) | (w, h)
                interpolation=interpolation_method,
            )

            # (cv2) BGR 2 (PIL) RGB. The ReID models have been trained with this channel order
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)

            crop = torch.from_numpy(crop).float()
            crops.append(crop)

        # List of torch tensor crops to unified torch tensor
        crops = torch.stack(crops, dim=0)

        # Normalize the batch
        crops = crops / 255.0

        # Standardize the batch
        crops = (crops - mean_array) / std_array

        crops = torch.permute(crops, (0, 3, 1, 2))
        crops = crops.to(dtype=torch.half if self.half else torch.float, device=self.device)

        return crops

    @torch.no_grad()
    def get_features(self, xyxys, img):
        if xyxys.size != 0:
            crops = self.get_crops(xyxys, img)
            crops = self.inference_preprocess(crops)
            features = self.forward(crops)
            features = self.inference_postprocess(features)
        else:
            features = np.array([])
        features = features / np.linalg.norm(features)
        return features

    def warmup(self, imgsz=[(256, 128, 3)]):
        # warmup model by running inference once
        if self.device.type != "cpu":
            im = np.random.randint(0, 255, *imgsz, dtype=np.uint8)
            im = self.preprocess(xyxys=np.array([[0, 0, 128, 256]]), img=im)
            self.forward(im)  # warmup

    def to_numpy(self, x):
        return x.cpu().numpy() if isinstance(x, torch.Tensor) else x

    def inference_preprocess(self, x):
        if self.half and x.dtype != torch.float16:
            x = x.half()
        if self.nhwc:
            x = x.permute(0, 2, 3, 1)
        return x
    
    def inference_postprocess(self, features):
        if isinstance(features, (list, tuple)):
            return (
                self.to_numpy(features[0]) if len(features) == 1 else [self.to_numpy(x) for x in features]
            )
        else:
            return self.to_numpy(features)

    @abstractmethod
    def forward(self, im_batch):
        pass

    @abstractmethod
    def load_model(self, w):
        pass


    def download_model(self, w):
        if w.suffix == ".pt":
            model_url = get_model_url(w)
            if not w.exists() and model_url is not None:
                gdown.download(model_url, str(w), quiet=False)
            elif w.exists():
                pass
            else:
                LOGGER.error(
                    f"No URL associated to the chosen StrongSORT weights ({w}). Choose between:"
                )
                show_downloadable_models()
                exit()