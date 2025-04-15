# InfluxDB Setup Notes

This project uses InfluxDB v2.x as its time-series database.

## Initial Setup (via Web UI or CLI)

After installing InfluxDB (e.g., `sudo apt install influxdb2`), access the web UI at `http://<your-server-ip>:8086`.

1.  **Create User:** Set up an initial administrator username and password.
2.  **Create Organization:** Create an Organization name (e.g., `FrostSystemOrg`). Note this name down, it's needed for configuration.
3.  **Create Bucket:** Create a Bucket to store the data (e.g., `FrostDataBucket`). Choose a retention policy (e.g., 30 days, 1 year, Forever). Note this name down.
4.  **Generate API Token:**
    *   Navigate to **Load Data** -> **API Tokens**.
    *   Click **Generate API Token** -> **All Access API Token**.
        *   *(Security Note: For production, it's better to create a custom token with specific read/write permissions only for the required bucket).*
    *   Give the token a description (e.g., `NodeRedWriteToken`).
    *   **IMPORTANT:** Copy the generated token string immediately and store it securely. You won't be able to see it again.
    *   This token needs to be configured in Node-RED and Grafana data sources.

## Required Settings for this Project

*   **Organization Name:** Used in Node-RED (`http request` node URL) and Grafana data source config.
*   **Bucket Name:** Used in Node-RED (`http request` node URL) and Grafana data source config.
*   **API Token:** Used in Node-RED (`http request` node header) and Grafana data source config (usually as `Authorization: Token YOUR_API_TOKEN` header).