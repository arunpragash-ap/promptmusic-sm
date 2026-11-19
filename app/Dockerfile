# PromptMusic Studio - small web GUI container (no model, no AWS CLI; talks to SageMaker via boto3).
#   docker build -t promptmusic-studio app/
#   docker run -p 7860:7860 -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
#     -e PM_USERS="alice:change-me" promptmusic-studio
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY studio.py .
ENV PM_HOST=0.0.0.0 PORT=7860 PM_OUTPUT_DIR=/tmp/studio AWS_REGION=ap-south-1 PYTHONUNBUFFERED=1
RUN useradd -m studio
USER studio
EXPOSE 7860
CMD ["python", "studio.py"]
