#  Build a harness
**This guide is for version 3 of the harness!**

## Getting Started

First, you need to identify where in the car the lane-keeping assist system's steering control commands are sent from. 
In most vehicles, this is the ADAS camera.
It's crucial to install the harness at a point that sits between the sender of the steering control commands and the rest of the car.
Most connectors have a part number. Check both the connector in the vehicle and the counterpart on the control unit. 

*Note:* Since molex [crimping tool]("https://www.digikey.com/en/products/detail/molex/0638192300/2413335") is expensive You can buy a pre-crimped development harness [here]("https://comma.ai/shop/harness-connector"). 

## Pinout

We are mainly interested in the CAN lines, ground, and ignition line of the connector. 
The connector should have all these signals. 12V is nice to have but optional. 
If the connector has no power lines, comma power can be used to supply power. 
If the car does not provide an ignition signal, there is a [workaround]("https://github.com/commaai/panda/blob/1cbcc13c35429b06e9d36c55b9ab46df73a8b51a/board/drivers/can_common.h#L198") in software to determine the ignition status using a CAN message.

It's best to find documentation like a repair manual to identify the correct pinout. 
Alternatively, a lot can be determined with a multimeter, such as power lines.
Using a resistance measurement, for example, you can measure the termination resistor of the camera, etc.

**Warning:** Do not try to determine the pinout by trial and error. You can cause significant damage!

## Connections

First open the [open pinout pdf](./v3/open_pinout.pdf). The basic setup is explained in the block diagram at the bottom right. 
It is important to understand that CAN2 and CAN0 are physically connected when the relay in the harness box is closed. 
At the point where the software triggers the relay to open, control messages from the camera on CAN2 are blocked and messages from "openpilot" are sent on CAN0.

First, we look at the connection between the ADAS camera and the harness box. This connection is called **CAN2**. 
Connect the CAN wire pair (CAN High & CAN Low) from the camera to the 18-pin Molex Connector; they are labeled "CAN2_H - ORANGE" and "CAN2_L - GREEN".

Next is the connection between the harness box and the car. This connection is called **CAN0**. 
This connection is labeled "CAN0_H - ORANGE" and "CAN0_L - GREEN".

We can also connect an additional CAN bus to our harness box, which is called **CAN1**.
In the pinout, this is found as "CAN1_L-BLUE" and "CAN1_H-PINK". We cannot intercept this CAN bus, but we can read and write messages from this bus in the software.
Typically, this CAN bus is connected to the radar. The harness box has two connections for CAN1, the wires are passed through. 

According to the documentation of the connector, we now also connect Ignition (IGN - BROWN), Ground (GND-BLACK), and if available, 12V (12VIN - RED). 

In the next step, it is important that all cables from the camera to the car connector that are not used must be passed through. 
For example, see the [Subaru A Harness](./v3/Subaru_A_Harness.pdf). The pass-through cables are labeled as PT1, PT2, PT3, etc.

In this final step, we address termination. Basically, a CAN bus requires termination (with 120 Ohm resistors) at the two physical endpoints of the CAN network.
You need to determine if the harness box must replace the ADAS camera's termination. 
To achieve this, install the resistor loopback wire between Pin 3 and Pin 5, which will terminate the CAN0 bus.
If the harness box doesn't need to terminate the bus, install a resistor loopback wire between Pin 13 and Pin 15.

**Important:** This guide refers to version 3 of the harness.

## Inspection
If you connect the harness box to your harness (relay is closed), the harness should function as a simple "extension".
You can now test with a multimeter in continuity mode to ensure each pin on one side is connected to the pin on the other side of the connector.
