# Car harness

<a href="https://www.youtube.com/watch?v=keM6UnpguKo" title="car harness"><img src="https://github.com/commaai/hardware/blob/master/harness/car-harness-includes.avif"></a>

## harness connector

The harness serves as the connection to the car. 
In most cars, it is typically connected to the ADAS camera, with only a few exceptions. 
The wiring diagrams for the harnesses we sell are open-sourced and can be found in [here](./v3).

Does your car model lack support from openpilot, and you’re eager to change that? The journey starts with [creating a custom harness](./BUILD_HARNESS.md)!

## harness box

The harness box is like an adapter between the car harness and the comma 3X. 
An integrated relay ensures that you can remove the c3x without your car giving you an error message.
The relay can be controlled by software and opens automatically when openpilot starts.
Internally, the camera's can bus is separated from the rest of the car. 
Some can messages between the camera and the car are simply passed through by software, but messages such as steering commands from the ADAS camera are intercepted and replaced by openpilot.

**The harness box must be connected to the harness, otherwise your car will display error messages.**

## obd-c

The [OBD-C Cable](https://comma.ai/shop/obd-c-cable) is a USB-C cable where all internal lines are fully connected, complying with the USB-C 3.1 Gen 2 or USB-C 3.2 Gen 2 specifications. 
The OBD-C pinout is specifed [here](./OBD-C.sch.pdf).
You might ask why are there short and long cables in the store? The long cable is typically used when the car harness is not connected at the adas camera near the comma 3X. 

All the exceptions are listed here on this [page]("https://www.comma.ai/shop/car-harness")

**NOTE: bad cables are common source of errors. To avoid unnecessary frustration, we recommend purchasing a [high-quality cable](https://comma.ai/shop/obd-c-cable) that meets the specifications.**

## comma power

comma power is an optional, yet recommended, addition to enhance your device's capabilities. 
The four-pin connection provides both power and OBD CAN connectivity.

While you can operate the device without comma power, it will only be powered while driving.
**With comma power** the device remains continuously powered, unlocking more features like a dashcam, remote tracking, SSH access, and more.
