"""The promoter: moves checked web deposits from staging/ to their
dataset prefix. Runs inside the KCL network with the real key, as its
own container on the internal VM - never in the web service's
container, which only ever holds the staging-only key.

Automated by default: every complete deposit whose checks all pass is
promoted; anything amiss is reported and left in staging. --dry-run
reviews without moving. Every action is logged."""
