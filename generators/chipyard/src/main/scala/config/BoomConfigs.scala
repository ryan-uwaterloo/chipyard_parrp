package chipyard

import org.chipsalliance.cde.config.{Config}
import parrp_chisel._
import verif.etrace._

// ---------------------
// BOOM Configs
// ---------------------

class TraceCosimConfig extends Config(
  new freechips.rocketchip.subsystem.BaseSubsystemConfig ++
  new verif.etrace.WithNTraces(4) ++
  new parrp_chisel.subsystem.WithInclusiveCache(nWays = 4, capacityKB = 8) ++
  new freechips.rocketchip.subsystem.WithoutTLMonitors)

class SmallBoomConfig extends Config(
  new boom.common.WithNSmallBooms(1) ++                          // small boom config
  new chipyard.config.AbstractConfig)

class MediumBoomConfig extends Config(
  new boom.common.WithNMediumBooms(1) ++                         // medium boom config
  new chipyard.config.AbstractConfig)

class LargeBoomConfig extends Config(
  new boom.common.WithNLargeBooms(1) ++                          // large boom config
  new chipyard.config.WithSystemBusWidth(128) ++
  new chipyard.config.AbstractConfig)

class MegaBoomConfig extends Config(
  new boom.common.WithNMegaBooms(1) ++                           // mega boom config
  new chipyard.config.WithSystemBusWidth(128) ++
  new chipyard.config.AbstractConfig)

class DualSmallBoomConfig extends Config(
  new boom.common.WithNSmallBooms(2) ++                          // 2 boom cores
  new parrp_chisel.subsystem.WithInclusiveCache ++       // local chisel code L2 cache
  new freechips.rocketchip.subsystem.WithoutTLMonitors ++
  //new chipyard.harness.WithSerialTLTiedOff ++ doesn't remove the monitor HW but makes it so you can't communicate with the UART
  new chipyard.config.AbstractConfig)

class DualLargeBoomConfig extends Config(
  new boom.common.WithNLargeBooms(2) ++                          // 2 boom cores
  new parrp_chisel.subsystem.WithInclusiveCache ++       // local chisel code L2 cache
  new chipyard.config.AbstractConfig)

class DualRTBoomConfig extends Config( //class for RT-capable BOOM platform
  new boom.common.WithNRTBooms(2) ++
  new parrp_chisel.subsystem.WithInclusiveCache(nWays = 4, capacityKB = 8) ++ //try dropping capacity a lot to get fewer sets and stuff
  new chipyard.config.AbstractConfig)

class PaarpOnlyConfig extends Config(
  new parrp_chisel.subsystem.WithInclusiveCache ++
  new chipyard.config.AbstractConfig)

class Cloned64MegaBoomConfig extends Config(
  new boom.common.WithCloneBoomTiles(63, 0) ++
  new boom.common.WithNMegaBooms(1) ++                           // mega boom config
  new chipyard.config.WithSystemBusWidth(128) ++
  new chipyard.config.AbstractConfig)

class LoopbackNICLargeBoomConfig extends Config(
  new chipyard.harness.WithLoopbackNIC ++                        // drive NIC IOs with loopback
  new icenet.WithIceNIC ++                                       // build a NIC
  new boom.common.WithNLargeBooms(1) ++
  new chipyard.config.WithSystemBusWidth(128) ++
  new chipyard.config.AbstractConfig)

class MediumBoomCosimConfig extends Config(
  new chipyard.harness.WithCospike ++                            // attach spike-cosim
  new chipyard.config.WithTraceIO ++                             // enable the traceio
  new boom.common.WithNMediumBooms(1) ++
  new chipyard.config.AbstractConfig)

class dmiMediumBoomConfig extends Config(
  new chipyard.harness.WithSerialTLTiedOff ++                    // don't attach anything to serial-tl
  new chipyard.config.WithDMIDTM ++                              // have debug module expose a clocked DMI port
  new boom.common.WithNMediumBooms(1) ++
  new chipyard.config.AbstractConfig)

class dmiMediumBoomCosimConfig extends Config(
  new chipyard.harness.WithCospike ++                            // attach spike-cosim
  new chipyard.config.WithTraceIO ++                             // enable the traceio
  new chipyard.harness.WithSerialTLTiedOff ++                    // don't attach anythint to serial-tl
  new chipyard.config.WithDMIDTM ++                              // have debug module expose a clocked DMI port
  new boom.common.WithNMediumBooms(1) ++
  new chipyard.config.AbstractConfig)
