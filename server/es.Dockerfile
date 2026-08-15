# Elasticsearch 8.17 with the analysis-ik Chinese tokenizer baked in.
# Plugin version must match the server version exactly (infinilabs builds
# per-version packages). offline builds: pre-download the zip and COPY it in.
FROM elasticsearch:8.17.0

RUN elasticsearch-plugin install --batch \
    https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.17.0.zip
