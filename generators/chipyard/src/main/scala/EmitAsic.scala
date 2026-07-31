package chipyard

import verif.etrace._
import verif.{MulticoreTraceTileHarness}

import org.scalatest.flatspec.AnyFlatSpec
import scala.collection.mutable
import firrtl.options.{Dependency, OptionsException, Phase, TargetDirAnnotation}

// import designs._
import chisel3._
import chisel3.util._
import chiseltest._
import chiseltest.simulator.{AsicEmitter}
// import chiseltest.experimental.TestOptionBuilder._
// import chiseltest.internal.{VerilatorBackendAnnotation, TreadleBackendAnnotation, WriteVcdAnnotation}
import org.chipsalliance.cde.config.Parameters
import freechips.rocketchip.tile.{RoCCCommand}
import freechips.rocketchip.tilelink.{TLBundleA}
import freechips.rocketchip.diplomacy.{TransferSizes, LazyModule}
import freechips.rocketchip.subsystem.{RocketCrossingParams}

import org.chipsalliance.cde.config.{Parameters, Config, Field}
import freechips.rocketchip.subsystem._
import freechips.rocketchip.devices.tilelink.{BootROMParams}
import freechips.rocketchip.diplomacy.{SynchronousCrossing, AsynchronousCrossing, RationalCrossing}
import freechips.rocketchip.rocket._
import freechips.rocketchip.tile._

import boom.common._
import boom.ifu._
// import boom.exu._
import boom.lsu._

object EmitAsic extends App {

    val config = new TraceCosimConfig
    implicit val params = config.toInstance

    val testHarness = LazyModule(
        new MulticoreTraceTileHarness(
        numTiles = 4,
        L2ways = 40,
        L2sets = 64,
        L2beatBytes = 16,
        L2blockBytes = 64
        )
    )

    AsicEmitter.emit(() => testHarness.module, "tools/siliconcompiler/build/asic")
}