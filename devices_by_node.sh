#!/bin/bash

for node in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    echo "Node: $node"
    kubectl get pods -l app=kathara --all-namespaces -o json | jq -r --arg NODE "$node" '.items[] | select(.spec.nodeName == $NODE) | .metadata.labels.name'
done