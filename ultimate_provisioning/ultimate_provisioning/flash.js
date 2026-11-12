// flash.py runs this from its qdl.js checkout: bun up_flash.js <programmer> <partition>=<image>...
// flashes each image to slot a of the DUT in QDL in one firehose session, then makes slot a active and resets, like commaai/flash
import { webusb } from "usb";
import { qdlDevice } from "./src/qdl.js";
import { usbClass } from "./src/usblib.js";

const [programmer, ...images] = process.argv.slice(2);
navigator.usb = webusb;
const qdl = new qdlDevice(await Bun.file(programmer).arrayBuffer());
await qdl.connect(new usbClass());

for (const arg of images) {
  const [name, path] = arg.split("=");
  const start = performance.now();
  if (!await qdl.flashBlob(`${name}_a`, Bun.file(path))) throw new Error(`flashing ${name}_a failed`);
  console.info(`flashed ${name}_a in ${((performance.now() - start) / 1000).toFixed(1)}s`);
}
if (!await qdl.setActiveSlot("a")) throw new Error("setting slot a active failed");
await qdl.reset();
process.exit(0);
