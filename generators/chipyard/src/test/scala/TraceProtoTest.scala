package chipyard

import verif.etrace._
import verif.{MulticoreTraceTileHarness}

import org.scalatest.flatspec.AnyFlatSpec
import scala.collection.mutable

// import designs._
import chisel3._
import chisel3.util._
import chiseltest._
import chiseltest.internal._
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

import java.io.FileInputStream
import java.util.zip.GZIPInputStream
import com.verif.TraceProtos._
import com.google.protobuf.CodedInputStream
import java.io.File

class DummyModule extends Module {
  val io = IO(new Bundle {})
}

case object NoHartLookup extends LookupByHartIdImpl {
  def apply[T <: Data](f: TileParams => Option[T], hartId: UInt): T = {
    require(false, "NoHartLookup should never be used to lookup any TileParams")
    null.asInstanceOf[T] // Needed to satisfy the return type, but should never be reached
  }
}

object TraceDataPath {
  def baseDir: String = {
    sys.props.get("TRACE_DIR")
      .orElse(sys.env.get("TRACE_DIR"))
      .getOrElse {
        // Use the build root, not current working directory
        new File(".").getCanonicalFile.getParentFile.getCanonicalPath + "/chipyard/traces"
      }
  }

  def path(subpath: String): String = {
    val file = new File(s"$baseDir/$subpath")
    require(file.exists(), s"Could not find trace file: ${file.getAbsolutePath}")
    file.getAbsolutePath
  }
}



class ProtoTest extends AnyFlatSpec with ChiselScalatestTester {

  def prepareTraces(testFolder: String, numTiles: Int, fromCsv: Boolean): Unit = {
    if (!fromCsv) return

    for (i <- 0 until numTiles) {
      CsvToProtoGz.convertCsv(
        csvPath   = TraceDataPath.path(s"csv/$testFolder/core_${i}_data.csv"),
        outputPath= s"${TraceDataPath.baseDir}/$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz",
        msgType   = "dcache"
      )

      CsvToProtoGz.convertCsv(
        csvPath   = TraceDataPath.path(s"csv/$testFolder/core_${i}_inst.csv"),
        outputPath= s"${TraceDataPath.baseDir}/$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz",
        msgType   = "icache"
      )
    }
  }

  def buildDAGs(
    testFolder: String,
    testName: String,
    numTiles: Int
  ) = {
    val dag = Seq.tabulate(numTiles){ i =>
      new ElasticTraceDAG(
        TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"),
        s"$testName.$i"
      )
    }

    val idag = Seq.tabulate(numTiles){ i =>
      new InstTraceDAG(
        TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"),
        s"$testName.$i"
      )
    }

    (dag, idag)
  }

  def runSimulation(
    dag: Seq[ElasticTraceDAG],
    idag: Seq[InstTraceDAG],
    numTiles: Int,
    l2ways: Int,
    traceVCD: Boolean,
    fromCsv: Boolean
  ): Unit = {
    var clock = 0L

    val config = new TraceCosimConfig
    implicit val params = config.toInstance

    val testHarness = LazyModule(
      new MulticoreTraceTileHarness(
        numTiles = numTiles,
        L2ways = l2ways,
        L2sets = 64,
        L2beatBytes = 16,
        L2blockBytes = 64
      )
    )

    val issued_a_req = mutable.Seq.fill(numTiles)(false)
    val inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

    var annotations = Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)
    if (traceVCD) {
      annotations = Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation, WriteVcdAnnotation)
    }

    test(testHarness.module).withAnnotations(annotations) { c =>

      c.clock.setTimeout(0)

      while (dag.exists(d => !d.isDone)) {
        clock += 1

        for (i <- 0 until numTiles) {
          // println(s"core $i here!")

          // Step 1: Advance software model
          dag(i).step()
          idag(i).step()
          issued_a_req(i) = false
          inst_issued_a_req(i) = false
          c.dcache_io(i).in.valid.poke(false.B)
          c.icache_io(i).in.valid.poke(false.B)

          // Step 2: Issue pending req if fifo ready
          try{
            c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
            Context().env.checkpoint() //commit point (readout errors now)

            dag(i).getPendingReq.foreach { req =>
              if(!issued_a_req(i)){ //if fifo is ready
                // println(s"dag $i is trying to send a req!")
                c.dcache_io(i).in.valid.poke(true.B)
                c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
                issued_a_req(i) = true //max 1 issue per cycle

                if(req.nodeType == LOAD){
                  // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
                  c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
                  c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
                  c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
                  c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
                  dag(i).issueLoad(req.seqNum)
                } else if(req.nodeType == STORE){
                  // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
                  c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
                  c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
                  c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
                  c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
                  c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
                  dag(i).issueStore(req.seqNum)
                }
              }
            }

          }catch{
            case e: FailedExpectException =>
              // println(s"[FIFO not ready!]")
              Context().env.batchedFailures.clear()
          }

          // increment request times for all requests before they reach cache
          dag(i).getPendingReq.foreach { req =>
            if(req.nodeType == LOAD){
              dag(i).incrementLoadTime(req.seqNum)
            } else if(req.nodeType == STORE){
              dag(i).incrementStoreTime(req.seqNum)
            }
          }

          // Step 3: Check if hardware acknowledged anything
          dag(i).getIssuedLoads.foreach { load =>
            try{
              c.dcache_io(i).out.valid.expect(true.B)
              c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
              c.dcache_io(i).out.bits.load_n_store.expect(true.B)
              Context().env.checkpoint()

              dag(i).acknowledgeLoad(load.seqNum)
              if (fromCsv) {
                if (clock > 50000) {
                  dag(i).log(s"DCache ${i}", load.seqNum)
                }
              } else {
                dag(i).log(s"DCache ${i}", load.seqNum)
              }
            }catch{
              case e: FailedExpectException =>
              dag(i).incrementLoadTime(load.seqNum)
              Context().env.batchedFailures.clear()
            }
          }
          dag(i).getIssuedStores.foreach { store =>
            try{
              c.dcache_io(i).out.valid.expect(true.B)
              c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
              c.dcache_io(i).out.bits.load_n_store.expect(false.B)
              Context().env.checkpoint()

              dag(i).acknowledgeStore(store.seqNum)
              if (fromCsv) {
                if (clock > 50000) {
                  dag(i).log(s"DCache ${i}", store.seqNum)
                }
              } else {
                dag(i).log(s"DCache ${i}", store.seqNum)
              }
            }catch{
              case e: FailedExpectException =>
              dag(i).incrementStoreTime(store.seqNum)
              Context().env.batchedFailures.clear()
            }
          }

          // Step 4: Issue ICache Req
          try{
            c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
            Context().env.checkpoint() //commit point (readout errors now)

            idag(i).getPendingReq.foreach { req =>
              if(!inst_issued_a_req(i)){ //if fifo is ready
                // println(s"idag $i is trying to send a req!")
                c.icache_io(i).in.valid.poke(true.B)
                c.icache_io(i).in.bits.addr.poke(req.addr.U)
                inst_issued_a_req(i) = true //max 1 issue per cycle

                // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
                c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
                c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
                c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
                c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
                idag(i).issueLoad(req.tick)
              }
            }

          }catch{
            case e: FailedExpectException =>
              // println(s"[FIFO not ready!]")
              Context().env.batchedFailures.clear()
          }

          // Step 5: Check for completions on idag(i)
          idag(i).getIssuedLoads.foreach { load =>
            try{
              c.icache_io(i).out.valid.expect(true.B)
              c.icache_io(i).out.bits.addr.expect(load.addr.U)
              c.icache_io(i).out.bits.load_n_store.expect(true.B)
              Context().env.checkpoint()

              idag(i).acknowledgeLoad(load.tick)
              if (fromCsv) {
                if (clock > 50000) {
                  idag(i).log(s"ICache ${i}", load.tick)
                }
              } else {
                idag(i).log(s"ICache ${i}", load.tick)
              }
            }catch{
              case e: FailedExpectException =>
              idag(i).incrementLoadTime(load.tick)
              Context().env.batchedFailures.clear()
            }
          }
        }

        c.clock.step()
      }

      c.clock.step(100)

      dag.foreach(_.closeLogger())
      idag.foreach(_.closeLogger())
    }
  }

  def runTraceTest(
  testFolder: String,
  testName: String,
  numTiles: Int,
  fromCsv: Boolean,
  l2ways: Int = 40,
  traceVCD: Boolean = false
): Unit = { 
  prepareTraces(testFolder, numTiles, fromCsv)

  val (dag, idag) = buildDAGs(testFolder, testName, numTiles)

  runSimulation(dag, idag, numTiles, l2ways, traceVCD, fromCsv)
}


it should "Run_radiosity-4" in {
  runTraceTest(
    testFolder = "radiosity-8",
    testName = "radiosity-4",
    numTiles = 4,
    fromCsv = false,
  )
}

it should "Run_radix-4" in {
  runTraceTest(
    testFolder = "radix-8",
    testName = "radix-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_barnes-4" in {
  runTraceTest(
    testFolder = "barnes-8",
    testName = "barnes-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_ocean-4" in {
  runTraceTest(
    testFolder = "ocean-8",
    testName = "ocean-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_fft-4" in {
  runTraceTest(
    testFolder = "fft-8",
    testName = "fft-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_fmm-4" in {
  runTraceTest(
    testFolder = "fmm-8",
    testName = "fmm-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_lu-4" in {
  runTraceTest(
    testFolder = "lu-8",
    testName = "lu-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_cholesky-4" in {
  runTraceTest(
    testFolder = "cholesky-8",
    testName = "cholesky-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_raytrace-4" in {
  runTraceTest(
    testFolder = "raytrace-8",
    testName = "raytrace-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_water-nsquared-4" in {
  runTraceTest(
    testFolder = "water-nsquared-8",
    testName = "water-nsquared-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_water-spatial-4" in {
  runTraceTest(
    testFolder = "water-spatial-8",
    testName = "water-spatial-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Run_nqueens-4" in {
  runTraceTest(
    testFolder = "nqueens-8",
    testName = "nqueens-4",
    numTiles = 4,
    fromCsv = false,
    traceVCD = false
  )
}

it should "Synthetic-nmshrs-4" in {
  runTraceTest(
    testFolder = "test_cases/nmshrs_test",
    testName = "nmshrs-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-hol-4" in {
  runTraceTest(
    testFolder = "test_cases/hol_test",
    testName = "hol-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-hol-8" in {
  runTraceTest(
    testFolder = "test_cases/hol_test",
    testName = "hol-8",
    numTiles = 8,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-probe-4" in {
  runTraceTest(
    testFolder = "test_cases/probe_test",
    testName = "probe-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-relbuf-4" in {
  runTraceTest(
    testFolder = "test_cases/releasebuf_test",
    testName = "relbuf-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-mempressure-4" in {
  runTraceTest(
    testFolder = "test_cases/releasebuf_test_diffsets",
    testName = "mempressure-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-inter-int-4" in {
  runTraceTest(
    testFolder = "test_cases/interference_test_int",
    testName = "inter-int-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

it should "Synthetic-inter-iso-4" in {
  runTraceTest(
    testFolder = "test_cases/interference_test_iso",
    testName = "inter-iso-4",
    numTiles = 4,
    fromCsv = true,
    traceVCD = true
  )
}

  // it should "Run_a_DAG_from_csv_synthetics" in {
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "test_cases/hol_test"
  //   val testName = "hol_test"

  //   for (i <- 0 until numTiles){
  //     CsvToProtoGz.convertCsv(
  //       csvPath = TraceDataPath.path(s"csv/${testFolder}/core_${i}_data.csv"),
  //       outputPath = s"${TraceDataPath.baseDir}/${testFolder}/system.cpu${i}.traceListener.data_trace.proto.gz",
  //       msgType = "dcache"
  //     )

  //     CsvToProtoGz.convertCsv(
  //       csvPath = TraceDataPath.path(s"csv/${testFolder}/core_${i}_inst.csv"),
  //       outputPath = s"${TraceDataPath.baseDir}/${testFolder}/system.cpu${i}.traceListener.inst_trace.proto.gz",
  //       msgType = "icache"
  //     )
  //   }

  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"), s"$testName.${i}")}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"), s"$testName.${i}")}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation, WriteVcdAnnotation)) { c =>
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // increment request times for all requests before they reach cache
  //         dag(i).getPendingReq.foreach { req =>
  //           if(req.nodeType == LOAD){
  //             dag(i).incrementLoadTime(req.seqNum)
  //           } else if(req.nodeType == STORE){
  //             dag(i).incrementStoreTime(req.seqNum)
  //           }
  //         }

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)

  //     dag.foreach(_.closeLogger())
  //     idag.foreach(_.closeLogger())
  //   }
  // }

  // it should "Run_ocean-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "ocean-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_radix-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "radix-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_fft-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "fft-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_lu-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "lu-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_cholesky-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "cholesky-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_barnes-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "barnes-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_fmm-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "fmm-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_radiosity-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "radiosity-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_raytrace-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "raytrace-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_water-nsquared-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "water-nsquared-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_water-spatial-4" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 4
  //   val testFolder = "water-spatial-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways  = 40, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_ocean-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "ocean-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_radix-8" in {
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "radix-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 80, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_fft-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "fft-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_lu-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "lu-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_cholesky-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "cholesky-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_barnes-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "barnes-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_fmm-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "fmm-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_radiosity-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "radiosity-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_raytrace-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "raytrace-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_water-nsquared-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "water-nsquared-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }

  // it should "Run_water-spatial-8" in {
  //   // throw new NotImplementedError("skipme!")
  //   var clock = 0x0L
  //   val numTiles = 8
  //   val testFolder = "water-spatial-8"
  //   val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"))}
  //   val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"))}
  //   val config = new TraceCosimConfig
  //   implicit val params = config.toInstance
  //   val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 72, L2sets = 64, L2beatBytes = 16, L2blockBytes = 64))
  //   var issued_a_req = mutable.Seq.fill(numTiles)(false)
  //   var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

  //   // val top = LazyModule((params(chipyard.BuildTop))(params))
  //   // test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //   test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, chiseltest.internal.NoThreadingAnnotation)) { c =>
  //     // test(testHarness.module) {c =>
  //     // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
  //     c.clock.setTimeout(0)
      
  //     while (dag.exists(d => !d.isDone)) {
  //       clock = clock + 1
  //       for(i <- 0 until numTiles){
  //         // println(s"core $i here!")
  //         // if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
  //         //   // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
  //         //   println(s"Hey here's what's throwing you for a loop in core $i:")
  //         //   dag(i).debug()
  //         //   // if(i == (numTiles-1)){
  //         //     throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
  //         //   // }
  //         // }

  //         // Step 1: Advance software model
  //         dag(i).step()
  //         idag(i).step()
  //         issued_a_req(i) = false
  //         inst_issued_a_req(i) = false
  //         c.dcache_io(i).in.valid.poke(false.B)
  //         c.icache_io(i).in.valid.poke(false.B)

  //         // Step 2: Issue pending req if fifo ready
  //         try{
  //           c.dcache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           dag(i).getPendingReq.foreach { req =>
  //             if(!issued_a_req(i)){ //if fifo is ready
  //               // println(s"dag $i is trying to send a req!")
  //               c.dcache_io(i).in.valid.poke(true.B)
  //               c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
  //               issued_a_req(i) = true //max 1 issue per cycle

  //               if(req.nodeType == LOAD){
  //                 // println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 dag(i).issueLoad(req.seqNum)
  //               } else if(req.nodeType == STORE){
  //                 // println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
  //                 c.dcache_io(i).in.bits.uop.uses_stq.poke(true.B)
  //                 c.dcache_io(i).in.bits.uop.uses_ldq.poke(false.B)
  //                 c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00001".U) //int store :)
  //                 c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //                 c.dcache_io(i).in.bits.data.poke(req.seqNum.U) //make up some random data :)
  //                 dag(i).issueStore(req.seqNum)
  //               }
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }     

  //         // Step 3: Check if hardware acknowledged anything
  //         dag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeLoad(load.seqNum)
  //             dag(i).log(s"DCache ${i}", load.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementLoadTime(load.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //         dag(i).getIssuedStores.foreach { store =>
  //           try{
  //             c.dcache_io(i).out.valid.expect(true.B)
  //             c.dcache_io(i).out.bits.addr.expect(store.pAddr.get.U)
  //             c.dcache_io(i).out.bits.load_n_store.expect(false.B)
  //             Context().env.checkpoint()

  //             dag(i).acknowledgeStore(store.seqNum)
  //             dag(i).log(s"DCache ${i}", store.seqNum)
  //           }catch{
  //             case e: FailedExpectException =>
  //             dag(i).incrementStoreTime(store.seqNum)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }

  //         // Step 4: Issue ICache Req
  //         try{
  //           c.icache_io(i).in.ready.expect(true.B) //can we issue a request?
  //           Context().env.checkpoint() //commit point (readout errors now)

  //           idag(i).getPendingReq.foreach { req =>
  //             if(!inst_issued_a_req(i)){ //if fifo is ready
  //               // println(s"idag $i is trying to send a req!")
  //               c.icache_io(i).in.valid.poke(true.B)
  //               c.icache_io(i).in.bits.addr.poke(req.addr.U)
  //               inst_issued_a_req(i) = true //max 1 issue per cycle

  //               // println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
  //               c.icache_io(i).in.bits.uop.uses_stq.poke(false.B)
  //               c.icache_io(i).in.bits.uop.uses_ldq.poke(true.B)
  //               c.icache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
  //               c.icache_io(i).in.bits.uop.mem_signed.poke(false.B)
  //               idag(i).issueLoad(req.tick)
  //             }
  //           }

  //         }catch{
  //           case e: FailedExpectException =>
  //             // println(s"[FIFO not ready!]")
  //             Context().env.batchedFailures.clear()
  //         }

  //         // Step 5: Check for completions on idag(i)
  //         idag(i).getIssuedLoads.foreach { load =>
  //           try{
  //             c.icache_io(i).out.valid.expect(true.B)
  //             c.icache_io(i).out.bits.addr.expect(load.addr.U)
  //             c.icache_io(i).out.bits.load_n_store.expect(true.B)
  //             Context().env.checkpoint()

  //             idag(i).acknowledgeLoad(load.tick)
  //             idag(i).log(s"ICache ${i}", load.tick)
  //           }catch{
  //             case e: FailedExpectException =>
  //             idag(i).incrementLoadTime(load.tick)
  //             Context().env.batchedFailures.clear()
  //           }
  //         }
  //       }

  //       // Step 6: Advance hardware clock
  //       c.clock.step()
  //       // if(clock == 40000L){
  //       //   for (i <- 0 until numTiles){
  //       //     println(s"Hey here's what's throwing you for a loop in core $i:")
  //       //     dag(i).debug()
  //       //   }
  //       //   throw new NotImplementedError("finish writing the cosimulator xddd")
  //       // }
  //       // if (clock % 1000 == 5){ //try to clean up some artifacts and fight a memory leak LOL
  //       //   c.backend.flush()
  //       // }
  //     }
  //     //run the simulator for another 100 cycles to clear residuals.
  //     c.clock.step(100)
  //   }
  // }
}