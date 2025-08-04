# Function deployment on Cloud Run

- Ensure project id is set correctly

gcloud config set project [project id]

- Deploy the function using gcloud. Make sure the main.py and requirements.txt files are in the same folder from where gcloud command is executed.

gcloud functions deploy scc-parser-publisher \
  --gen2 \
  --runtime=python311 \
  --region=us-central1 \
  --source=. \
  --entry-point=process_pubsub_message \
  --set-env-vars GCP_PROJECT=$(gcloud config get-value project) \
  --trigger-topic=scc-findings


# Application Integration process

The json file in this repository can be imported as automation process to the gcp application integration module.
This process trigers every time a new scc evet/message (parsed and sent by the funtion) gets into a pub/sub topic, after mapping the data an alert email will be sent to the recipient configure in the "Send Email" task.
