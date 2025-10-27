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
  //implicit val p: Parameters = VerifTestUtils.getVerifParameters()

  // it should "Protobuf parse dcache message from file" in {
  //   test(new DummyModule) {_ =>
  //   // Load and decompress the file
  //   val stream = getClass.getResourceAsStream("/system.cpu0.traceListener.data_trace.proto.gz")
  //   require(stream != null, "Could not find trace file!")

  //   val gzipStream = new GZIPInputStream(stream)
  //   val codedInput = CodedInputStream.newInstance(gzipStream)

  //   // STEP 1: Skip the 4-byte magic number
  //   codedInput.readRawLittleEndian32() // discard it

  //   // STEP 2: Read the first message (should be the header)
  //   val headerSize = codedInput.readRawVarint32()
  //   val headerLimit = codedInput.pushLimit(headerSize)
  //   val header = InstDepRecordHeader.parseFrom(codedInput)
  //   codedInput.popLimit(headerLimit)

  //   println(s"Header: tickFreq=${header.getTickFreq}, windowSize=${header.getWindowSize}")

  //   // STEP 3: Read the rest of the delimited messages
  //   var count = 0
  //   while (count < 10) {
  //     try {
  //       val msgSize = codedInput.readRawVarint32()
  //       val limit = codedInput.pushLimit(msgSize)
  //       val record = InstDepRecord.parseFrom(codedInput)
  //       codedInput.popLimit(limit)

  //       println(record)
  //       count += 1
  //     } catch {
  //       case e: Exception =>
  //         println(s"Failed to parse record $count: ${e.getMessage}")
  //         throw e
  //     }
  //   }

  //   println(s"Parsed $count records.")
  //   gzipStream.close()
  //   }
  // }

  // it should "Protobuf parse icache message from file" in {
  //   test(new DummyModule) {_ =>
  //   // Load and decompress the file
  //   val stream = getClass.getResourceAsStream("/system.cpu0.traceListener.inst_trace.proto.gz")
  //   require(stream != null, "Could not find trace file!")

  //   val gzipStream = new GZIPInputStream(stream)
  //   val codedInput = CodedInputStream.newInstance(gzipStream)

  //   // STEP 1: Skip the 4-byte magic number
  //   codedInput.readRawLittleEndian32() // discard it (it's gem5 :))

  //   // STEP 2: Read the first message (should be the header)
  //   val headerSize = codedInput.readRawVarint32()
  //   val headerLimit = codedInput.pushLimit(headerSize)
  //   val header = PacketHeader.parseFrom(codedInput)
  //   codedInput.popLimit(headerLimit)

  //   println(s"Header: tickFreq=${header.getTickFreq}")//", windowSize=${header.getWindowSize}")

  //   // STEP 3: Read the rest of the delimited messages
  //   var count = 0
  //   while (count < 10) {
  //     try {
  //       val msgSize = codedInput.readRawVarint32()
  //       val limit = codedInput.pushLimit(msgSize)
  //       val record = Packet.parseFrom(codedInput)
  //       codedInput.popLimit(limit)

  //       println(record)
  //       count += 1
  //     } catch {
  //       case e: Exception =>
  //         println(s"Failed to parse record $count: ${e.getMessage}")
  //         throw e
  //     }
  //   }

  //   println(s"Parsed $count records.")
  //   gzipStream.close()
  //   }
  // }

  it should "Run DAG against Chisel hardware" in {
    // throw new NotImplementedError("skipme!")
    var clock = 0x0L
    val numTiles = 4
    val testFolder = "raytrace-8"
    val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"), 100)}
    val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"), 1)}
    val config = new TraceCosimConfig
    implicit val params = config.toInstance
    val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 4, L2sets = 4, L2beatBytes = 8, L2blockBytes = 32))
    var issued_a_req = mutable.Seq.fill(numTiles)(false)
    var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

    // val top = LazyModule((params(chipyard.BuildTop))(params))
    test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation)) { c =>
    // test(testHarness.module) {c =>
    // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
    c.clock.setTimeout(0)
    
    while (dag.exists(d => !d.isDone)) {
      for(i <- 0 until numTiles){
        // println(s"core $i here!")
        if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
          // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
          println(s"Hey here's what's throwing you for a loop in core $i:")
          dag(i).debug()
          // if(i == (numTiles-1)){
            throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
          // }
        }
        clock = clock + 1

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
              println(s"dag $i is trying to send a req!")
              c.dcache_io(i).in.valid.poke(true.B)
              c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
              issued_a_req(i) = true //max 1 issue per cycle

              if(req.nodeType == LOAD){
                println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
                c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
                c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
                c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
                c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
                dag(i).issueLoad(req.seqNum)
              } else if(req.nodeType == STORE){
                println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
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

        // Step 3: Check if hardware acknowledged anything
        dag(i).getIssuedLoads.foreach { load =>
          try{
            c.dcache_io(i).out.valid.expect(true.B)
            c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
            c.dcache_io(i).out.bits.load_n_store.expect(true.B)
            Context().env.checkpoint()

            dag(i).acknowledgeLoad(load.seqNum)
            dag(i).log(s"DCache ${i}", load.seqNum)
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
            dag(i).log(s"DCache ${i}", store.seqNum)
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
              println(s"idag $i is trying to send a req!")
              c.icache_io(i).in.valid.poke(true.B)
              c.icache_io(i).in.bits.addr.poke(req.addr.U)
              inst_issued_a_req(i) = true //max 1 issue per cycle

              println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
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
            idag(i).log(s"ICache ${i}", load.tick)
          }catch{
            case e: FailedExpectException =>
            idag(i).incrementLoadTime(load.tick)
            Context().env.batchedFailures.clear()
          }
        }
      }

      // Step 6: Advance hardware clock
      c.clock.step()
      // if(clock == 1000000L){
      //   throw new NotImplementedError("finish writing the cosimulator xddd")
      // }
    }

    println("Simulation completed.")
    }
  }

  it should "Run_a_DAG_from_csv_synthetics" in {
    var clock = 0x0L
    val numTiles = 4
    val testFolder = "synthetic_sanity_test"

    for (i <- 0 until numTiles){
      CsvToProtoGz.convertCsv(
        csvPath = TraceDataPath.path(s"csv/${testFolder}/core_${i}_data.csv"),
        outputPath = s"${TraceDataPath.baseDir}/${testFolder}/system.cpu${i}.traceListener.data_trace.proto.gz",
        msgType = "dcache"
      )

      CsvToProtoGz.convertCsv(
        csvPath = TraceDataPath.path(s"csv/${testFolder}/core_${i}_inst.csv"),
        outputPath = s"${TraceDataPath.baseDir}/${testFolder}/system.cpu${i}.traceListener.inst_trace.proto.gz",
        msgType = "icache"
      )
    }

    val dag = Seq.tabulate(numTiles){i => new ElasticTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.data_trace.proto.gz"), 6)}
    val idag = Seq.tabulate(numTiles){i =>new InstTraceDAG(TraceDataPath.path(s"$testFolder/system.cpu${i}.traceListener.inst_trace.proto.gz"), 3)}
    val config = new TraceCosimConfig
    implicit val params = config.toInstance
    val testHarness = LazyModule(new MulticoreTraceTileHarness(numTiles = numTiles, L2ways = 4, L2sets = 4, L2beatBytes = 8, L2blockBytes = 32))
    var issued_a_req = mutable.Seq.fill(numTiles)(false)
    var inst_issued_a_req = mutable.Seq.fill(numTiles)(false)

    // val top = LazyModule((params(chipyard.BuildTop))(params))
    test(testHarness.module).withAnnotations(Seq(VerilatorBackendAnnotation, WriteVcdAnnotation)) { c =>
      // test(testHarness.module) {c =>
      // val traceTileCore0 = new TraceTile(params, RocketCrossingParams(), NoHartLookup)
      c.clock.setTimeout(0)
      
      while (dag.exists(d => !d.isDone)) {
        for(i <- 0 until numTiles){
          // println(s"core $i here!")
          if(idag(i).isDone){ //if we finish our accesses after itrace things are BAD!
            // println("!!!!! INSTRUCTION dag(i) COMPLETE !!!!!")
            println(s"Hey here's what's throwing you for a loop in core $i:")
            dag(i).debug()
            // if(i == (numTiles-1)){
              throw new NotImplementedError(s"!!!!! INSTRUCTION dag${i} COMPLETE !!!!!")
            // }
          }
          clock = clock + 1

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
                println(s"dag $i is trying to send a req!")
                c.dcache_io(i).in.valid.poke(true.B)
                c.dcache_io(i).in.bits.addr.poke(req.pAddr.get.U)
                issued_a_req(i) = true //max 1 issue per cycle

                if(req.nodeType == LOAD){
                  println(s"@ Cycle ${clock} Issuing LOAD ${req.seqNum} to hardware")
                  c.dcache_io(i).in.bits.uop.uses_stq.poke(false.B)
                  c.dcache_io(i).in.bits.uop.uses_ldq.poke(true.B)
                  c.dcache_io(i).in.bits.uop.mem_cmd.poke("b00000".U) //int load :)
                  c.dcache_io(i).in.bits.uop.mem_signed.poke(false.B)
                  dag(i).issueLoad(req.seqNum)
                } else if(req.nodeType == STORE){
                  println(s"@ Cycle ${clock} Issuing STORE ${req.seqNum} to hardware")
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

          // Step 3: Check if hardware acknowledged anything
          dag(i).getIssuedLoads.foreach { load =>
            try{
              c.dcache_io(i).out.valid.expect(true.B)
              c.dcache_io(i).out.bits.addr.expect(load.pAddr.get.U)
              c.dcache_io(i).out.bits.load_n_store.expect(true.B)
              Context().env.checkpoint()

              dag(i).acknowledgeLoad(load.seqNum)
              dag(i).log(s"DCache ${i}", load.seqNum)
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
              dag(i).log(s"DCache ${i}", store.seqNum)
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
                println(s"idag $i is trying to send a req!")
                c.icache_io(i).in.valid.poke(true.B)
                c.icache_io(i).in.bits.addr.poke(req.addr.U)
                inst_issued_a_req(i) = true //max 1 issue per cycle

                println(s"@ Cycle ${clock} Issuing I-LOAD ${req.tick} to hardware")
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
              idag(i).log(s"ICache ${i}", load.tick)
            }catch{
              case e: FailedExpectException =>
              idag(i).incrementLoadTime(load.tick)
              Context().env.batchedFailures.clear()
            }
          }
        }

        // Step 6: Advance hardware clock
        c.clock.step()
        // if(clock == 1000000L){
        //   throw new NotImplementedError("finish writing the cosimulator xddd")
      }
    }
  }

}