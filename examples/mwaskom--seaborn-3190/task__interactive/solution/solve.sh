#!/bin/bash
# Oracle control: apply the reference patch in place. Harbor's oracle agent uploads /solution and runs this script.
set -e
cd /testbed
git apply --whitespace=nowarn /solution/oracle.diff
