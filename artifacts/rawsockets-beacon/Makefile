# Makefile — build the raw-socket beacon
# Run `make` to build, `make cap` to grant CAP_NET_RAW so it can run without
# root each time, `make clean` to remove the binary.

CC      := gcc
CFLAGS  := -Wall -Wextra -O2 -std=gnu99
TARGET  := beacon

.PHONY: all cap clean

all: $(TARGET)

$(TARGET): beacon.c
	$(CC) $(CFLAGS) -o $@ beacon.c

# Grant the raw-socket capability so the beacon does not need full root.
# This is the *least privilege* way to run it for your firewall test.
cap: $(TARGET)
	sudo setcap cap_net_raw+ep $(TARGET)

clean:
	rm -f $(TARGET)
