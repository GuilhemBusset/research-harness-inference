# Grafana OSS behind the load balancer's sign-in (system design, section 7.4).
# The upstream image is pinned by digest; an update is a change to this line.
FROM grafana/grafana-oss:13.0.2@sha256:5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2

COPY grafana.ini /etc/grafana/grafana.ini
COPY provisioning/ /etc/grafana/provisioning/
# The dashboards are built from the deployed configuration at synthesis and
# reach this folder of the build context; in the repository it holds none.
COPY dashboards/ /etc/grafana/dashboards/

# Grafana's own default port, which the load balancer's target group uses.
EXPOSE 3000
