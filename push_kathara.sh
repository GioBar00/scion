docker tag endhost:kathara registry.digitalocean.com/scion-on-kubernetes/endhost:kathara
docker tag control:kathara registry.digitalocean.com/scion-on-kubernetes/control:kathara
docker tag posix-router:kathara registry.digitalocean.com/scion-on-kubernetes/posix-router:kathara

docker push registry.digitalocean.com/scion-on-kubernetes/endhost:kathara
docker push registry.digitalocean.com/scion-on-kubernetes/control:kathara
docker push registry.digitalocean.com/scion-on-kubernetes/posix-router:kathara