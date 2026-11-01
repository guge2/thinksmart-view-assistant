#!/bin/sh
set -eu

QUP_PATH=/sys/bus/platform/devices/78b5000.i2c
DRIVER_PATH=/sys/bus/platform/drivers/i2c_qup
DEVICE=78b5000.i2c

if [ -e "$DRIVER_PATH/unbind" ]; then
	printf '%s' "$DEVICE" > "$DRIVER_PATH/unbind"
	sleep 1
fi

if [ -e "$DRIVER_PATH/bind" ]; then
	printf '%s' "$DEVICE" > "$DRIVER_PATH/bind"
	sleep 1
fi

if [ -e "$QUP_PATH/power/control" ]; then
	printf '%s' on > "$QUP_PATH/power/control"
fi

modprobe snd_soc_tas5782m

