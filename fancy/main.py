# Two functions (deploy on cloud run) for processing scc notifications sent to a specific pub/sub topic,
# Log the notifications as json documents on Firestore and second function pick up the documents, process
# and send them as consolidated alerts to another pub/sub topic where integration task will follow to send emails from the alerts.

import json
import base64
from google.cloud import pubsub_v1, firestore
import os
from collections import defaultdict

# --- CLIENT INITIALIZATION ---
# Initialize clients outside function handlers for connection reuse and performance.
publisher = pubsub_v1.PublisherClient()
db = firestore.Client()

PROJECT_ID = os.getenv('GCP_PROJECT')
DESTINATION_TOPIC = 'scc-alerts-2send' # Topic for the final, aggregated report
FIRESTORE_COLLECTION = 'pending_scc_findings' # Firestore collection to store findings


# --- FUNCTION 1: PROCESS AND STORE INDIVIDUAL FINDINGS ---
# cloud run deploy: gcloud functions deploy scc-findings-ingestor --gen2 --runtime=python311 --region=us-central1 --source=. --entry-point=process_and_store_finding --trigger-topic=scc-findings --set-env-vars GCP_PROJECT=$(gcloud config get-value project)
def process_and_store_finding(event, context):
    """
    Triggered by a new finding. Parses the finding and stores it in Firestore.
    """
    print(f"Ingestor function triggered by messageId: {context.event_id}")

    try:
        pubsub_message = base64.b64decode(event['data']).decode('utf-8')
        pubsub_data = json.loads(pubsub_message)
        finding = pubsub_data.get('finding', {})
        resource = pubsub_data.get('resource', {})
    except Exception as e:
        print(f"Error decoding or parsing initial message: {e}")
        return 'Error: Malformed input message', 400

    # The unique name of the finding is a perfect candidate for the document ID
    # to prevent processing the same finding notification twice.
    finding_id = finding.get('name')
    if not finding_id:
        print("Error: Finding data is missing the 'name' field, cannot create a unique ID.")
        return 'Error: Missing finding name', 400
    
    # Sanitize the finding_id to be a valid Firestore document ID
    document_id = finding_id.replace('/', '_')

    # Parse all the relevant data
    finding_data = {
        'title': finding.get('category', 'N/A'),
        'severity': finding.get('severity', 'N/A'),
        'project_name': resource.get('gcpMetadata', {}).get('projectDisplayName', 'N/A'),
        'resource': resource.get('displayName', 'N/A'), # Kept the original long name for reference
        'full_resource_name': finding.get('resourceName', 'N/A'),
        'description': finding.get('description', 'N/A'),
        'next_steps': finding.get('nextSteps', 'N/A'),
        'event_time': finding.get('eventTime', 'N/A'),
        'finding_id': finding_id,
        # These keys are not in this specific JSON, but the safe .get() method handles them.
        'explanation': finding.get('sourceProperties', {}).get('Explanation', 'N/A'),
        'external_uri': finding.get('externalUri', 'N/A')
    }

    # Store the parsed finding in Firestore
    try:
        doc_ref = db.collection(FIRESTORE_COLLECTION).document(document_id)
        doc_ref.set(finding_data)
        print(f"Successfully stored finding {document_id} in Firestore.")
    except Exception as e:
        print(f"Error writing to Firestore: {e}")
        raise # Re-raise to signal an execution failure

    return 'OK', 204


# --- FUNCTION 2: AGGREGATE AND PUBLISH DIGEST ALERT ---
# cloud run deploy: gcloud functions deploy scc-digest-publisher --gen2 --runtime=python311 --region=us-central1 --source=. --entry-point=digest_publisher --trigger-topic=scc-findings-parsed --set-env-vars GCP_PROJECT=$(gcloud config get-value project)
def digest_publisher(event, context):
    """
    Triggered by Cloud Scheduler. Aggregates findings from Firestore, formats
    the timestamps, and publishes a summary.
    """
    print("SCC alerts digest_publisher function triggered.")
    
    findings_ref = db.collection(FIRESTORE_COLLECTION)
    all_findings_docs = list(findings_ref.stream())

    if not all_findings_docs:
        print("No pending findings to process. Exiting.")
        return 'OK', 204

    # Group findings by (resource, title)
    grouped_findings = defaultdict(list)
    for doc in all_findings_docs:
        finding = doc.to_dict()
        key = (finding.get('resource'), finding.get('title'))
        grouped_findings[key].append(finding)
    
    print(f"Found {len(all_findings_docs)} findings, aggregated into {len(grouped_findings)} groups.")

    # Create and publish a summary for each group
    topic_path = publisher.topic_path(PROJECT_ID, DESTINATION_TOPIC)
    publish_futures = []

    for (resource_name, title), findings_list in grouped_findings.items():
        # --- Logic to format dates and times ---
        datetimes = []
        for finding in findings_list:
            event_time_str = finding.get('event_time', '')
            if event_time_str and 'T' in event_time_str:
                # Format to "YYYY-MM-DD HH:MM:SS UTC"
                parts = event_time_str.split('T')
                time_part = parts[1][:8] # Grabs HH:MM:SS
                formatted_dt = f"{parts[0]} {time_part} UTC"
                datetimes.append(formatted_dt)
        
        # Create a unique, sorted list of dates and times and join with HTML line breaks
        occurrence_datetimes_html = "<br>".join(sorted(list(set(datetimes))))

        next_steps_raw = findings_list[0].get('next_steps', 'No remediation steps available')
        
        # Create one detailed summary message per group
        summary_message = {
            "aggregation_key": {
                "resource": resource_name,
                "title": title
            },
            "count": len(findings_list),
            "severity": findings_list[0].get('severity'),
            "project_name": findings_list[0].get('project_name'),
            "description": findings_list[0].get('description', 'No description available.'),
            "next_steps": next_steps_raw,
            "formatted_next_steps": next_steps_raw.replace('\n', '<br>'),
            "occurrences": findings_list,
            "occurrence_datetimes": occurrence_datetimes_html
        }
        
        message_data = json.dumps(summary_message).encode('utf-8')
        future = publisher.publish(topic_path, data=message_data)
        publish_futures.append(future)
    
    for future in publish_futures:
        future.result()
    print(f"Successfully published {len(grouped_findings)} summary messages.")

    batch = db.batch()
    for doc in all_findings_docs:
        batch.delete(doc.reference)
    batch.commit()
    print(f"Successfully deleted {len(all_findings_docs)} processed findings from Firestore.")

    return 'OK', 204