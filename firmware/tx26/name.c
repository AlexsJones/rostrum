// USB names, so it shows up as "TX-26" in sound settings instead of "Teensy MIDI/Audio".
#include "usb_names.h"

#define MANUFACTURER_NAME {'R','o','s','t','r','u','m'}
#define MANUFACTURER_NAME_LEN 7
#define PRODUCT_NAME {'T','X','-','2','6'}
#define PRODUCT_NAME_LEN 5

struct usb_string_descriptor_struct usb_string_manufacturer_name = {
  2 + MANUFACTURER_NAME_LEN * 2, 3, MANUFACTURER_NAME
};
struct usb_string_descriptor_struct usb_string_product_name = {
  2 + PRODUCT_NAME_LEN * 2, 3, PRODUCT_NAME
};
