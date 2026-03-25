#!/bin/bash
# Deploy ovrtx-render to RunPod serverless
#
# Creates template (via GraphQL API) and endpoint (via runpodctl)
#
# Why GraphQL for template?
# runpodctl template create --serverless has a bug: it sends volumeInGb
# even when not specified, and the API rejects serverless templates with volumeInGb.

set -e

if [ -z "$RUNPOD_API_KEY" ]; then
  if [ -f .env ]; then
    source .env
  fi
fi

if [ -z "$RUNPOD_API_KEY" ]; then
  echo "Error: RUNPOD_API_KEY not set"
  echo "Either export RUNPOD_API_KEY or add it to .env"
  exit 1
fi

# Handler code - read from ovrtx_render.py (single source of truth)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDLER_B64=$(base64 -i "$SCRIPT_DIR/ovrtx_render.py" | tr -d '\n')

# Build dockerArgs
DOCKER_ARGS="bash -lc \"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb libvulkan1 vulkan-tools mesa-vulkan-drivers && python3 -m pip install --no-cache-dir runpod requests pillow numpy && python3 -m pip install --no-cache-dir https://pypi.nvidia.com/ovrtx/ovrtx-0.2.0.280040-py3-none-manylinux_2_35_x86_64.whl && echo ${HANDLER_B64} | base64 -d > /tmp/handler.py && python3 -u /tmp/handler.py\""

# Create GraphQL payload
cat << EOF > /tmp/create_template.json
{
  "query": "mutation CreateTemplate(\$input: SaveTemplateInput!) { saveTemplate(input: \$input) { id name imageName isServerless containerDiskInGb } }",
  "variables": {
    "input": {
      "name": "ovrtx-render-v6",
      "imageName": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
      "containerDiskInGb": 50,
      "volumeInGb": 0,
      "isServerless": true,
      "dockerArgs": $(echo "$DOCKER_ARGS" | jq -Rs .),
      "env": [],
      "readme": "# ovrtx-render API\n\n## Input\n- usdz_url (required)\n- distance_multiplier (default: 3.0)\n- azimuth (default: 45.0)\n- elevation (default: 30.0)\n- width (default: 1920)\n- height (default: 1080)\n- warmup_frames (default: 10)\n- format (default: png)",
      "startJupyter": false,
      "startSsh": false,
      "volumeMountPath": "/workspace"
    }
  }
}
EOF

echo "Creating template via GraphQL API..."
RESULT=$(curl -s -X POST https://api.runpod.io/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -d @/tmp/create_template.json)

echo "$RESULT" | jq .

TEMPLATE_ID=$(echo "$RESULT" | jq -r '.data.saveTemplate.id')

if [ "$TEMPLATE_ID" = "null" ] || [ -z "$TEMPLATE_ID" ]; then
  echo "Failed to create template"
  exit 1
fi

echo ""
echo "Template created: $TEMPLATE_ID"
echo ""
echo "Creating endpoint..."

ENDPOINT_RESULT=$(RUNPOD_API_KEY=$RUNPOD_API_KEY runpodctl serverless create \
  --name "ovrtx-render" \
  --template-id "$TEMPLATE_ID" \
  --gpu-id "NVIDIA GeForce RTX 5090" \
  --workers-min 0 \
  --workers-max 2)

echo "$ENDPOINT_RESULT" | jq .

ENDPOINT_ID=$(echo "$ENDPOINT_RESULT" | jq -r '.id')

if [ "$ENDPOINT_ID" = "null" ] || [ -z "$ENDPOINT_ID" ]; then
  echo "Failed to create endpoint"
  exit 1
fi

echo ""
echo "=========================================="
echo "Deployment complete!"
echo "=========================================="
echo "Template ID:  $TEMPLATE_ID"
echo "Endpoint ID:  $ENDPOINT_ID"
echo "Endpoint URL: https://api.runpod.ai/v2/$ENDPOINT_ID/runsync"
echo ""
echo "Test with:"
echo "curl -X POST \"https://api.runpod.ai/v2/$ENDPOINT_ID/runsync\" \\"
echo "  -H \"Authorization: Bearer \$RUNPOD_API_KEY\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -d '{\"input\": {\"usdz_url\": \"https://developer.apple.com/augmented-reality/quick-look/models/teapot/teapot.usdz\"}}'"
