HIRES_SCALE_BY = 1.5
HIRES_DENOISE = 0.5

FACE_DETECTOR_MODEL = "bbox/face_yolov8m.pt"
FACE_DETAILER_DENOISE = 0.3


def build_workflow(spec: dict) -> tuple[dict, str]:
    """Builds a ComfyUI workflow graph from a generation spec.

    Returns (workflow, save_node_id).
    - spec["hires"]: appends a latent-upscale + refine KSampler pass (real
      hi-res fix, not just larger base dimensions).
    - spec["face_fix"]: appends a face-detect-and-refine pass (ComfyUI Impact
      Pack's FaceDetailer) after whichever image is currently final. Safe to
      combine with hires (runs after it) or use alone; a no-op if no face is
      detected in the frame.
    - spec["loras"]: list of {"name", "strength"}, chained as LoraLoader nodes
      between the checkpoint and every downstream model/clip consumer.
    Both hires/face_fix are optional and compose: base -> [hires] -> [face_fix] -> save.
    """
    scheduler = spec.get("scheduler", "normal")

    workflow = {
        "3": {
            "inputs": {"ckpt_name": spec["checkpoint"]},
            "class_type": "CheckpointLoaderSimple",
        },
    }

    model_ref = ["3", 0]
    clip_ref = ["3", 1]
    for i, lora in enumerate(spec.get("loras") or []):
        node_id = f"2{i}"
        workflow[node_id] = {
            "inputs": {
                "lora_name": lora["name"],
                "strength_model": lora["strength"],
                "strength_clip": lora["strength"],
                "model": model_ref,
                "clip": clip_ref,
            },
            "class_type": "LoraLoader",
        }
        model_ref = [node_id, 0]
        clip_ref = [node_id, 1]

    workflow["1"] = {
        "inputs": {"text": spec["positive_prompt"], "clip": clip_ref},
        "class_type": "CLIPTextEncode",
    }
    workflow["2"] = {
        "inputs": {"text": spec["negative_prompt"], "clip": clip_ref},
        "class_type": "CLIPTextEncode",
    }
    workflow["4"] = {
        "inputs": {
            "seed": spec["seed"],
            "steps": spec["steps"],
            "cfg": spec["cfg"],
            "sampler_name": spec["sampler_name"],
            "scheduler": scheduler,
            "denoise": 1.0,
            "model": model_ref,
            "positive": ["1", 0],
            "negative": ["2", 0],
            "latent_image": ["5", 0],
        },
        "class_type": "KSampler",
    }
    workflow["5"] = {
        "inputs": {
            "width": spec["width"],
            "height": spec["height"],
            "batch_size": 1,
        },
        "class_type": "EmptyLatentImage",
    }

    if spec.get("hires"):
        workflow["8"] = {
            "inputs": {
                "samples": ["4", 0],
                "upscale_method": "bislerp",
                "scale_by": HIRES_SCALE_BY,
            },
            "class_type": "LatentUpscaleBy",
        }
        workflow["9"] = {
            "inputs": {
                "seed": spec["seed"],
                "steps": spec["steps"],
                "cfg": spec["cfg"],
                "sampler_name": spec["sampler_name"],
                "scheduler": scheduler,
                "denoise": HIRES_DENOISE,
                "model": model_ref,
                "positive": ["1", 0],
                "negative": ["2", 0],
                "latent_image": ["8", 0],
            },
            "class_type": "KSampler",
        }
        workflow["10"] = {
            "inputs": {"samples": ["9", 0], "vae": ["3", 2]},
            "class_type": "VAEDecode",
        }
        image_node = ["10", 0]
    else:
        workflow["6"] = {
            "inputs": {"samples": ["4", 0], "vae": ["3", 2]},
            "class_type": "VAEDecode",
        }
        image_node = ["6", 0]

    if spec.get("face_fix"):
        workflow["12"] = {
            "inputs": {"model_name": FACE_DETECTOR_MODEL},
            "class_type": "UltralyticsDetectorProvider",
        }
        workflow["13"] = {
            "inputs": {
                "image": image_node,
                "model": model_ref,
                "clip": clip_ref,
                "vae": ["3", 2],
                "guide_size": 512,
                "guide_size_for": True,
                "max_size": 1024,
                "seed": spec["seed"],
                "steps": spec["steps"],
                "cfg": spec["cfg"],
                "sampler_name": spec["sampler_name"],
                "scheduler": scheduler,
                "positive": ["1", 0],
                "negative": ["2", 0],
                "denoise": FACE_DETAILER_DENOISE,
                "feather": 5,
                "noise_mask": True,
                "force_inpaint": True,
                "bbox_threshold": 0.5,
                "bbox_dilation": 10,
                "bbox_crop_factor": 3.0,
                "sam_detection_hint": "center-1",
                "sam_dilation": 0,
                "sam_threshold": 0.93,
                "sam_bbox_expansion": 0,
                "sam_mask_hint_threshold": 0.7,
                "sam_mask_hint_use_negative": "False",
                "drop_size": 10,
                "bbox_detector": ["12", 0],
                "wildcard": "",
                "cycle": 1,
            },
            "class_type": "FaceDetailer",
        }
        image_node = ["13", 0]

    save_id = "7"
    workflow[save_id] = {
        "inputs": {"images": image_node, "filename_prefix": "nl_gen"},
        "class_type": "SaveImage",
    }
    return workflow, save_id
