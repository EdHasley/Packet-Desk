#!/bin/sh
set -eu
cd "$(dirname "$0")"
version=$(sed -n 's/^Version: //p' package/DEBIAN/control)
mkdir -p dist
dpkg-deb --root-owner-group --build package "dist/packet-desk_${version}_all.deb"
