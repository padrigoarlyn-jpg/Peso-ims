import io
import os
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError


ALLOWED_JOB_FAIR_IMAGE_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "webp"
}

JOB_FAIR_BANNER_SIZE = (1600, 600)
MAX_JOB_FAIR_IMAGE_BYTES = 8 * 1024 * 1024
MAX_JOB_FAIR_IMAGE_PIXELS = 40_000_000


def allowed_job_fair_image(filename):
    """Check whether the uploaded job fair image has an allowed extension."""
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_JOB_FAIR_IMAGE_EXTENSIONS
    )


def save_job_fair_banner(uploaded_file, upload_folder):
    """
    Process and save a job fair poster/banner as a standardized
    1600 x 600 JPEG image.

    The original aspect ratio is preserved.
    Excess portions are center-cropped instead of stretched.
    """

    if not uploaded_file or not uploaded_file.filename:
        return None

    if not allowed_job_fair_image(uploaded_file.filename):
        raise ValueError(
            "Invalid image format. Please upload JPG, JPEG, PNG, or WEBP."
        )

    try:
        uploaded_file.stream.seek(0)
        data = uploaded_file.stream.read(MAX_JOB_FAIR_IMAGE_BYTES + 1)
        uploaded_file.stream.seek(0)

        if not data:
            raise ValueError("The uploaded image is empty.")

        if len(data) > MAX_JOB_FAIR_IMAGE_BYTES:
            raise ValueError("The job fair poster must not exceed 8 MB.")

        probe = Image.open(io.BytesIO(data))
        width, height = probe.size
        if width <= 0 or height <= 0 or width * height > MAX_JOB_FAIR_IMAGE_PIXELS:
            raise ValueError("The uploaded image dimensions are too large.")
        probe.verify()

        image = Image.open(io.BytesIO(data))

        # Correct phone/camera EXIF rotation
        image = ImageOps.exif_transpose(image)

        # Convert transparent or non-RGB images
        if image.mode in ("RGBA", "LA"):
            background = Image.new(
                "RGB",
                image.size,
                (255, 255, 255)
            )

            alpha = image.getchannel("A")
            background.paste(image, mask=alpha)

            image = background

        elif image.mode != "RGB":
            image = image.convert("RGB")

        # Resize + center crop to exactly 1600 x 600
        image = ImageOps.fit(
            image,
            JOB_FAIR_BANNER_SIZE,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5)
        )

        os.makedirs(upload_folder, exist_ok=True)

        # Always save the processed banner as JPEG
        filename = f"jobfair_{uuid.uuid4().hex}.jpg"

        filepath = os.path.join(
            upload_folder,
            filename
        )

        image.save(
            filepath,
            format="JPEG",
            quality=90,
            optimize=True
        )

        return filename

    except UnidentifiedImageError:
        raise ValueError(
            "The uploaded file is not a valid image."
        )

    except OSError:
        raise ValueError(
            "The uploaded image could not be processed."
        )