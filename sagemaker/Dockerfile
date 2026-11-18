# One image for prepare (Processing), train (Training) and serve (Endpoint).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PROMPTMUSIC_HOME=/opt/program HF_HOME=/tmp/hf

RUN apt-get update \
 && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/program
COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
COPY data ./data
COPY tests ./tests

# Never let pip replace the CUDA build of torch that ships in the base image.
# (DLC wheels show up in `pip freeze` as "torch @ file://...", so read the versions instead.)
COPY sagemaker/pin_torch.py /tmp/pin_torch.py
RUN python /tmp/pin_torch.py > /tmp/constraints.txt \
 && cat /tmp/constraints.txt \
 && pip install --no-cache-dir -c /tmp/constraints.txt -e ".[analysis]" \
      "transformers>=4.44,<5" sentencepiece==0.2.2 flask==3.0.3 gunicorn==22.0.0 \
      pytest pytest-timeout \
 && python -c "import torch, promptmusic; print('torch', torch.__version__, 'cuda', torch.version.cuda)"

# Build gate: the offline unit tests must pass inside the real image.
RUN python -m pytest -q -m "not slow" -p no:cacheprovider tests

COPY sagemaker ./sagemaker
RUN chmod +x sagemaker/dispatch.sh && ln -s /opt/program/sagemaker/dispatch.sh /usr/local/bin/dispatch.sh
ENTRYPOINT ["/usr/local/bin/dispatch.sh"]
