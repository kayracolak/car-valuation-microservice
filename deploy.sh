#!/bin/bash
# Kubernetes Deploy Script
# Kullanım: bash deploy.sh
# Gereksinim: kubectl + çalışan bir K8s cluster (minikube veya gerçek)

set -e

echo "========================================"
echo "  Araç Değerleme Sistemi - K8s Deploy"
echo "========================================"

# Minikube kullanıyorsanız local imajlar için:
# eval $(minikube docker-env)
# docker compose build

echo ""
echo "→ Namespace oluşturuluyor..."
kubectl apply -f k8s/namespace.yaml

echo "→ ConfigMap ve Secret uygulanıyor..."
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml

echo "→ RabbitMQ başlatılıyor..."
kubectl apply -f k8s/rabbitmq.yaml
echo "   RabbitMQ hazır olana kadar bekleniyor (max 90s)..."
kubectl wait --for=condition=ready pod \
  -l app=rabbitmq \
  -n araba-degerleme \
  --timeout=90s

echo "→ Servisler başlatılıyor..."
kubectl apply -f k8s/auth-service.yaml
kubectl apply -f k8s/valuation-service.yaml
kubectl apply -f k8s/notification-service.yaml
kubectl apply -f k8s/api-gateway.yaml

echo ""
echo "→ Deploymentlar hazır olana kadar bekleniyor..."
kubectl rollout status deployment/auth-service -n araba-degerleme
kubectl rollout status deployment/valuation-service -n araba-degerleme
kubectl rollout status deployment/api-gateway -n araba-degerleme

echo ""
echo "========================================"
echo "  Deploy tamamlandı!"
echo "========================================"
kubectl get pods -n araba-degerleme
echo ""
echo "API Gateway erişimi için:"
echo "  minikube: minikube service api-gateway -n araba-degerleme"
echo "  port-forward: kubectl port-forward svc/api-gateway 8080:8080 -n araba-degerleme"
