// flash.py runs this from its qdl.js checkout: bun up_flash.js <programmer> <partition>=<image>...
// flashes each image to both slots of the DUT in QDL in one firehose session, then makes slot a active and resets, like commaai/flash
import { webusb } from "usb";
import { qdlDevice } from "./src/qdl.js";
import { usbClass } from "./src/usblib.js";

const [programmer, ...images] = process.argv.slice(2);
navigator.usb = webusb;
const qdl = new qdlDevice(await Bun.file(programmer).arrayBuffer());
await qdl.connect(new usbClass());

for (const arg of images) {
  const [name, path] = arg.split("=");
  for (const slot of ["a", "b"]) {
    const start = performance.now();
    if (!await qdl.flashBlob(`${name}_${slot}`, Bun.file(path))) throw new Error(`flashing ${name}_${slot} failed`);
    console.info(`flashed ${name}_${slot} in ${((performance.now() - start) / 1000).toFixed(1)}s`);
  }
}
if (!await qdl.setActiveSlot("a")) throw new Error("setting slot a active failed");
await qdl.reset();
process.exit(0);
