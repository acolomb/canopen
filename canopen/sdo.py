import collections
import struct
import logging
import io
import binascii
import time
try:
    import queue
except ImportError:
    import Queue as queue

from .network import CanError

from . import objectdictionary
from . import common


logger = logging.getLogger(__name__)


# Command, index, subindex
SDO_STRUCT = struct.Struct("<BHB")


REQUEST_SEGMENT_DOWNLOAD = 0 << 5
REQUEST_DOWNLOAD = 1 << 5
REQUEST_UPLOAD = 2 << 5
REQUEST_SEGMENT_UPLOAD = 3 << 5
REQUEST_ABORTED = 4 << 5
REQUEST_BLOCK_UPLOAD = 5 << 5
REQUEST_BLOCK_DOWNLOAD = 6 << 5

RESPONSE_SEGMENT_UPLOAD = 0 << 5
RESPONSE_SEGMENT_DOWNLOAD = 1 << 5
RESPONSE_UPLOAD = 2 << 5
RESPONSE_DOWNLOAD = 3 << 5
RESPONSE_ABORTED = 4 << 5
RESPONSE_BLOCK_DOWNLOAD = 5 << 5
RESPONSE_BLOCK_UPLOAD = 6 << 5

INITIATE_BLOCK_TRANSFER = 0
END_BLOCK_TRANSFER = 1
BLOCK_TRANSFER_RESPONSE = 2
START_BLOCK_UPLOAD = 3

EXPEDITED = 0x2
SIZE_SPECIFIED = 0x1
BLOCK_SIZE_SPECIFIED = 0x2
CRC_SUPPORTED = 0x4
NO_MORE_DATA = 0x1
NO_MORE_BLOCKS = 0x80
TOGGLE_BIT = 0x10


class SdoClient(collections.Mapping):
    """Handles communication with an SDO server."""

    #: Max time in seconds to wait for response from server
    RESPONSE_TIMEOUT = 0.3

    #: Max number of request retries before raising error
    MAX_RETRIES = 1

    def __init__(self, rx_cobid, tx_cobid, od):
        """
        :param int rx_cobid:
            COB-ID that the server receives on (usually 0x600 + node ID)
        :param int tx_cobid:
            COB-ID that the server responds with (usually 0x580 + node ID)
        :param canopen.ObjectDictionary od:
            Object Dictionary to use for communication
        """
        self.rx_cobid = rx_cobid
        self.tx_cobid = tx_cobid
        self.network = None
        self.od = od
        self.pause_before_send = 0.0
        self.responses = queue.Queue()

    def on_response(self, can_id, data, timestamp):
        self.responses.put(bytes(data))

    def send_request(self, request):
        retries_left = self.MAX_RETRIES
        while True:
            try:
                if self.pause_before_send:
                    time.sleep(self.pause_before_send)
                self.network.send_message(self.rx_cobid, request)
            except CanError as e:
                # Could be a buffer overflow. Wait some time before trying again
                retries_left -= 1
                if not retries_left:
                    raise
                logger.info(str(e))
                time.sleep(0.1)
            else:
                break

    def read_response(self):
        try:
            response = self.responses.get(
                block=True, timeout=self.RESPONSE_TIMEOUT)
        except queue.Empty:
            raise SdoCommunicationError("No SDO response received")
        res_command, = struct.unpack_from("B", response)
        if res_command == RESPONSE_ABORTED:
            abort_code, = struct.unpack_from("<L", response, 4)
            raise SdoAbortedError(abort_code)
        return response

    def request_response(self, sdo_request):
        retries_left = self.MAX_RETRIES
        if not self.responses.empty():
            #logger.warning("There were unexpected messages in the queue")
            self.responses = queue.Queue()
        while True:
            self.send_request(sdo_request)
            # Wait for node to respond
            try:
                return self.read_response()
            except SdoCommunicationError as e:
                retries_left -= 1
                if not retries_left:
                    raise
                logger.warning(str(e))

    def abort(self, abort_code=0x08000000):
        """Abort current transfer."""
        request = bytearray(8)
        request[0] = REQUEST_ABORTED
        # TODO: Is it necessary to include index and subindex?
        struct.pack_into("<L", request, 4, abort_code)
        self.send_request(request)
        logger.error("Transfer aborted by client with code 0x{:08X}".format(abort_code))

    def upload(self, index, subindex):
        """May be called to make a read operation without an Object Dictionary.

        :param int index:
            Index of object to read.
        :param int subindex:
            Sub-index of object to read.

        :return: A data object.
        :rtype: bytes

        :raises canopen.SdoCommunicationError:
            On unexpected response or timeout.
        :raises canopen.SdoAbortedError:
            When node responds with an error.
        """
        with ReadableStream(self, index, subindex) as fp:
            return fp.read()

    def download(self, index, subindex, data, force_segment=False):
        """May be called to make a write operation without an Object Dictionary.

        :param int index:
            Index of object to write.
        :param int subindex:
            Sub-index of object to write.
        :param bytes data:
            Data to be written.
        :param bool force_segment:
            Force use of segmented transfer regardless of data size.

        :raises canopen.SdoCommunicationError:
            On unexpected response or timeout.
        :raises canopen.SdoAbortedError:
            When node responds with an error.
        """
        raw_stream = WritableStream(self, index, subindex, len(data), force_segment)
        fp = io.BufferedWriter(raw_stream, 7)
        fp.write(data)
        fp.close()

    def __getitem__(self, index):
        entry = self.od[index]
        if isinstance(entry, objectdictionary.Variable):
            return Variable(self, entry)
        elif isinstance(entry, objectdictionary.Array):
            return Array(self, entry)
        elif isinstance(entry, objectdictionary.Record):
            return Record(self, entry)

    def __iter__(self):
        return iter(self.od)

    def __len__(self):
        return len(self.od)

    def __contains__(self, key):
        return key in self.od


class Record(collections.Mapping):

    def __init__(self, sdo_node, od):
        self.sdo_node = sdo_node
        self.od = od

    def __getitem__(self, subindex):
        return Variable(self.sdo_node, self.od[subindex])

    def __iter__(self):
        return iter(self.od)

    def __len__(self):
        return len(self.od)

    def __contains__(self, subindex):
        return subindex in self.od


class Array(collections.Mapping):

    def __init__(self, sdo_node, od):
        self.sdo_node = sdo_node
        self.od = od

    def __getitem__(self, subindex):
        return Variable(self.sdo_node, self.od[subindex])

    def __iter__(self):
        return iter(range(1, len(self) + 1))

    def __len__(self):
        return self[0].raw

    def __contains__(self, subindex):
        return 0 <= subindex <= len(self)


class Variable(common.Variable):
    """Access object dictionary variable values using SDO protocol."""

    def __init__(self, sdo_node, od):
        self.sdo_node = sdo_node
        common.Variable.__init__(self, od)

    def get_data(self):
        return self.sdo_node.upload(self.od.index, self.od.subindex)

    def set_data(self, data):
        force_segment = self.od.data_type == objectdictionary.DOMAIN
        self.sdo_node.download(self.od.index, self.od.subindex, data, force_segment)

    def read(self, fmt="raw"):
        """Alternative way of reading using a function instead of attributes.

        May be useful for asynchronous reading.

        :param str fmt:
            How to return the value
             - 'raw'
             - 'phys'
             - 'desc'

        :returns:
            The value of the variable.
        """
        if fmt == "raw":
            return self.raw
        elif fmt == "phys":
            return self.phys
        elif fmt == "desc":
            return self.desc

    def write(self, value, fmt="raw"):
        """Alternative way of writing using a function instead of attributes.

        May be useful for asynchronous writing.

        :param str fmt:
            How to write the value
             - 'raw'
             - 'phys'
             - 'desc'
        """
        if fmt == "raw":
            self.raw = value
        elif fmt == "phys":
            self.phys = value
        elif fmt == "desc":
            self.desc = value

    def open(self, mode="rb", encoding="ascii", buffering=1024, size=None,
             block_transfer=False):
        """Open the data stream as a file like object.

        :param str mode:
            ========= ==========================================================
            Character Meaning
            --------- ----------------------------------------------------------
            'r'       open for reading (default)
            'w'       open for writing
            'b'       binary mode (default)
            't'       text mode
            ========= ==========================================================
        :param str encoding:
            The str name of the encoding used to decode or encode the file.
            This will only be used in text mode.
        :param int buffering:
            An optional integer used to set the buffering policy. Pass 0 to
            switch buffering off (only allowed in binary mode), 1 to select line
            buffering (only usable in text mode), and an integer > 1 to indicate
            the size in bytes of a fixed-size chunk buffer.
        :param int size:
            Size of data to that will be transmitted.
        :param bool block_transfer:
            If block transfer should be used.

        :returns:
            A file like object.
        """
        buffer_size = buffering if buffering > 1 else io.DEFAULT_BUFFER_SIZE
        if "r" in mode:
            if block_transfer:
                raw_stream = BlockUploadStream(
                    self.sdo_node, self.od.index, self.od.subindex)
            else:
                raw_stream = ReadableStream(
                    self.sdo_node, self.od.index, self.od.subindex)
            if buffering:
                buffered_stream = io.BufferedReader(raw_stream, buffer_size=buffer_size)
            else:
                return raw_stream
        if "w" in mode:
            if block_transfer:
                raw_stream = BlockDownloadStream(
                    self.sdo_node, self.od.index, self.od.subindex, size)
            else:
                raw_stream = WritableStream(
                    self.sdo_node, self.od.index, self.od.subindex, size)
            if buffering:
                buffered_stream = io.BufferedWriter(raw_stream, buffer_size=buffer_size)
            else:
                return raw_stream
        if "b" not in mode:
            # Text mode
            line_buffering = buffering == 1
            return io.TextIOWrapper(buffered_stream, encoding,
                                    line_buffering=line_buffering)
        return buffered_stream


class ReadableStream(io.RawIOBase):
    """File like object for reading from a variable."""

    #: Total size of data or ``None`` if not specified
    size = None

    def __init__(self, sdo_client, index, subindex=0):
        """
        :param canopen.sdo.SdoClient sdo_client:
            The SDO client to use for reading.
        :param int index:
            Object dictionary index to read from.
        :param int subindex:
            Object dictionary sub-index to read from.
        """
        self._done = False
        self.sdo_client = sdo_client
        self._toggle = 0
        self.pos = 0

        logger.debug("Reading 0x%X:%d from node %d", index, subindex,
                     sdo_client.rx_cobid - 0x600)
        request = bytearray(8)
        SDO_STRUCT.pack_into(request, 0, REQUEST_UPLOAD, index, subindex)
        response = sdo_client.request_response(request)
        res_command, res_index, res_subindex = SDO_STRUCT.unpack_from(response)
        res_data = response[4:8]

        if res_command & 0xE0 != RESPONSE_UPLOAD:
            raise SdoCommunicationError("Unexpected response 0x%02X" % res_command)

        # Check that the message is for us
        if res_index != index or res_subindex != subindex:
            raise SdoCommunicationError((
                "Node returned a value for 0x{:X}:{:d} instead, "
                "maybe there is another SDO client communicating "
                "on the same SDO channel?").format(res_index, res_subindex))

        self.exp_data = None
        if res_command & EXPEDITED:
            # Expedited upload
            if res_command & SIZE_SPECIFIED:
                self.size = 4 - ((res_command >> 2) & 0x3)
                self.exp_data = res_data[:self.size]
            else:
                self.exp_data = res_data
            self.pos += len(self.exp_data)
        elif res_command & SIZE_SPECIFIED:
            self.size, = struct.unpack("<L", res_data)
            logger.debug("Using segmented transfer of %d bytes", self.size)
        else:
            logger.debug("Using segmented transfer")

    def read(self, size=-1):
        """Read one segment which may be up to 7 bytes.

        :param int size:
            If size is -1, all data will be returned. Other values are ignored.

        :returns: 1 - 7 bytes of data or no bytes if EOF.
        :rtype: bytes
        """
        if self._done:
            return b""
        if self.exp_data is not None:
            self._done = True
            return self.exp_data
        if size is None or size < 0:
            return self.readall()

        command = REQUEST_SEGMENT_UPLOAD
        command |= self._toggle
        request = bytearray(8)
        request[0] = command
        response = self.sdo_client.request_response(request)
        res_command, = struct.unpack_from("B", response)
        if res_command & 0xE0 != RESPONSE_SEGMENT_UPLOAD:
            raise SdoCommunicationError("Unexpected response 0x%02X" % res_command)
        if res_command & TOGGLE_BIT != self._toggle:
            raise SdoCommunicationError("Toggle bit mismatch")
        length = 7 - ((res_command >> 1) & 0x7)
        if res_command & NO_MORE_DATA:
            self._done = True
        self._toggle ^= TOGGLE_BIT
        self.pos += length
        return response[1:length + 1]

    def readinto(self, b):
        """
        Read bytes into a pre-allocated, writable bytes-like object b,
        and return the number of bytes read.
        """
        data = self.read(7)
        b[:len(data)] = data
        return len(data)

    def readable(self):
        return True

    def tell(self):
        return self.pos


class WritableStream(io.RawIOBase):
    """File like object for writing to a variable."""

    def __init__(self, sdo_client, index, subindex=0, size=None, force_segment=False):
        """
        :param canopen.sdo.SdoClient sdo_client:
            The SDO client to use for communication.
        :param int index:
            Object dictionary index to read from.
        :param int subindex:
            Object dictionary sub-index to read from.
        :param int size:
            Size of data in number of bytes if known in advance.
        :param bool force_segment:
            Force use of segmented transfer regardless of size.
        """
        self.sdo_client = sdo_client
        self.size = size
        self.pos = 0
        self._toggle = 0
        self._exp_header = None
        self._done = False

        if size is None or size > 4 or force_segment:
            # Initiate segmented download
            request = bytearray(8)
            command = REQUEST_DOWNLOAD
            if size is not None:
                command |= SIZE_SPECIFIED
                struct.pack_into("<L", request, 4, size)
            SDO_STRUCT.pack_into(request, 0, command, index, subindex)
            response = sdo_client.request_response(request)
            res_command, = struct.unpack_from("B", response)
            if res_command != RESPONSE_DOWNLOAD:
                raise SdoCommunicationError(
                    "Unexpected response 0x%02X" % res_command)
        else:
            # Expedited download
            # Prepare header (first 4 bytes in CAN message)
            command = REQUEST_DOWNLOAD | EXPEDITED | SIZE_SPECIFIED
            command |= (4 - size) << 2
            self._exp_header = SDO_STRUCT.pack(command, index, subindex)

    def write(self, b):
        """
        Write the given bytes-like object, b, to the SDO server, and return the
        number of bytes written. This will be at most 7 bytes.
        """
        if self._done:
            raise RuntimeError("All expected data has already been transmitted")
        if self._exp_header is not None:
            # Expedited download
            if len(b) < self.size:
                # Not enough data provided
                return 0
            if len(b) > 4:
                raise AssertionError("More data received than expected")
            data = b.tobytes() if isinstance(b, memoryview) else b
            request = self._exp_header + data.ljust(4, b"\x00")
            response = self.sdo_client.request_response(request)
            res_command, = struct.unpack_from("B", response)
            if res_command & 0xE0 != RESPONSE_DOWNLOAD:
                raise SdoCommunicationError(
                    "Unexpected response 0x%02X" % res_command)
            bytes_sent = len(b)
            self._done = True
        else:
            # Segmented download
            request = bytearray(8)
            command = REQUEST_SEGMENT_DOWNLOAD
            # Add toggle bit
            command |= self._toggle
            self._toggle ^= TOGGLE_BIT
            # Can send up to 7 bytes at a time
            bytes_sent = min(len(b), 7)
            if self.size is not None and self.pos + bytes_sent >= self.size:
                # No more data after this message
                command |= NO_MORE_DATA
                self._done = True
            # Specify number of bytes that do not contain segment data
            command |= (7 - bytes_sent) << 1
            request[0] = command
            request[1:bytes_sent + 1] = b[0:bytes_sent]
            response = self.sdo_client.request_response(request)
            res_command, = struct.unpack("B", response[0:1])
            if res_command & 0xE0 != RESPONSE_SEGMENT_DOWNLOAD:
                raise SdoCommunicationError(
                    "Unexpected response 0x%02X (expected 0x%02X)" % res_command)
        # Advance position
        self.pos += bytes_sent
        return bytes_sent

    def close(self):
        """Closes the stream.

        An empty segmented SDO message may be sent saying there is no more data.
        """
        super(WritableStream, self).close()
        if not self._done and not self._exp_header:
            # Segmented download not finished
            command = REQUEST_SEGMENT_DOWNLOAD | NO_MORE_DATA
            command |= self._toggle
            # No data in this message
            command |= 7 << 1
            request = bytearray(8)
            request[0] = command
            self.sdo_client.request_response(request)
            self._done = True

    def writable(self):
        return True

    def tell(self):
        return self.pos


class BlockUploadStream(io.RawIOBase):
    """File like object for reading from a variable using block upload."""

    #: Total size of data or ``None`` if not specified
    size = None

    blksize = 127

    crc_supported = False

    def __init__(self, sdo_client, index, subindex=0):
        """
        :param canopen.sdo.SdoClient sdo_client:
            The SDO client to use for reading.
        :param int index:
            Object dictionary index to read from.
        :param int subindex:
            Object dictionary sub-index to read from.
        """
        self._done = False
        self.sdo_client = sdo_client
        self.pos = 0
        self._crc = 0
        self._server_crc = None
        self._ackseq = 0

        logger.debug("Reading 0x%X:%d from node %d", index, subindex,
                     sdo_client.rx_cobid - 0x600)
        # Initiate Block Upload
        request = bytearray(8)
        command = REQUEST_BLOCK_UPLOAD | INITIATE_BLOCK_TRANSFER | CRC_SUPPORTED
        struct.pack_into("<BHBBB", request, 0,
                         command, index, subindex, self.blksize, 0)
        response = sdo_client.request_response(request)
        res_command, res_index, res_subindex = SDO_STRUCT.unpack_from(response)
        if res_command & 0xE0 != RESPONSE_BLOCK_UPLOAD:
            raise SdoCommunicationError("Unexpected response 0x%02X" % res_command)
        # Check that the message is for us
        if res_index != index or res_subindex != subindex:
            raise SdoCommunicationError((
                "Node returned a value for 0x{:X}:{:d} instead, "
                "maybe there is another SDO client communicating "
                "on the same SDO channel?").format(res_index, res_subindex))
        if res_command & BLOCK_SIZE_SPECIFIED:
            self.size, = struct.unpack_from("<L", response, 4)
            logger.debug("Size is %d bytes", self.size)
        self.crc_supported = bool(res_command & CRC_SUPPORTED)
        # Start upload
        request = bytearray(8)
        request[0] = REQUEST_BLOCK_UPLOAD | START_BLOCK_UPLOAD
        sdo_client.send_request(request)

    def read(self, size=-1):
        """Read one segment which may be up to 7 bytes.

        :param int size:
            If size is -1, all data will be returned. Other values are ignored.

        :returns: 1 - 7 bytes of data or no bytes if EOF.
        :rtype: bytes
        """
        if self._done:
            return b""
        if size is None or size < 0:
            return self.readall()

        try:
            response = self.sdo_client.read_response()
        except SdoCommunicationError:
            response = self._retransmit()
        res_command, = struct.unpack_from("B", response)
        seqno = res_command & 0x7F
        if seqno == self._ackseq + 1:
            self._ackseq = seqno
        else:
            # Wrong sequence number
            response = self._retransmit()
            res_command, = struct.unpack_from("B", response)
        if self._ackseq >= self.blksize or res_command & NO_MORE_BLOCKS:
            self._ack_block()
        if res_command & NO_MORE_BLOCKS:
            n = self._end_upload()
            data = response[1:8 - n]
            self._done = True
        else:
            data = response[1:8]
        if self.crc_supported:
            self._crc = binascii.crc_hqx(data, self._crc)
            if self._done:
                if self._server_crc != self._crc:
                    self.sdo_client.abort(0x05040004)
                    raise SdoCommunicationError("CRC is not OK")
                logger.info("CRC is OK")
        self.pos += len(data)
        return data

    def _retransmit(self):
        logger.info("Only %d sequences were received. Requesting retransmission",
                    self._ackseq)
        end_time = time.time() + self.sdo_client.RESPONSE_TIMEOUT
        self._ack_block()
        while time.time() < end_time:
            response = self.sdo_client.read_response()
            res_command, = struct.unpack_from("B", response)
            seqno = res_command & 0x7F
            if seqno == self._ackseq + 1:
                # We should be back in sync
                self._ackseq = seqno
                return response
        raise SdoCommunicationError("Some data were lost and could not be retransmitted")

    def _ack_block(self):
        request = bytearray(8)
        request[0] = REQUEST_BLOCK_UPLOAD | BLOCK_TRANSFER_RESPONSE
        request[1] = self._ackseq
        request[2] = self.blksize
        self.sdo_client.send_request(request)
        if self._ackseq == self.blksize:
            self._ackseq = 0

    def _end_upload(self):
        response = self.sdo_client.read_response()
        res_command, self._server_crc = struct.unpack_from("<BH", response)
        if res_command & 0xE0 != RESPONSE_BLOCK_UPLOAD:
            self.sdo_client.abort(0x05040001)
            raise SdoCommunicationError("Unexpected response 0x%02X" % res_command)
        if res_command & 0x3 != END_BLOCK_TRANSFER:
            self.sdo_client.abort(0x05040001)
            raise SdoCommunicationError("Server did not end transfer as expected")
        # Return number of bytes not used in last message
        return (res_command >> 2) & 0x7

    def close(self):
        if self.closed:
            return
        super(BlockUploadStream, self).close()
        if self._done:
            request = bytearray(8)
            request[0] = REQUEST_BLOCK_UPLOAD | END_BLOCK_TRANSFER
            self.sdo_client.send_request(request)

    def tell(self):
        return self.pos

    def readinto(self, b):
        """
        Read bytes into a pre-allocated, writable bytes-like object b,
        and return the number of bytes read.
        """
        data = self.read(7)
        b[:len(data)] = data
        return len(data)

    def readable(self):
        return True


class BlockDownloadStream(io.RawIOBase):
    """File like object for block download."""

    def __init__(self, sdo_client, index, subindex=0, size=None):
        """
        :param canopen.sdo.SdoClient sdo_client:
            The SDO client to use for communication.
        :param int index:
            Object dictionary index to read from.
        :param int subindex:
            Object dictionary sub-index to read from.
        :param int size:
            Size of data in number of bytes if known in advance.
        """
        self.sdo_client = sdo_client
        self.size = size
        self.pos = 0
        self._done = False
        self._seqno = 0
        self._crc = 0
        self._last_bytes_sent = 0
        command = REQUEST_BLOCK_DOWNLOAD | INITIATE_BLOCK_TRANSFER | CRC_SUPPORTED
        request = bytearray(8)
        logger.info("Initiating block download for 0x%X:%d", index, subindex)
        if size is not None:
            logger.debug("Expected size of data is %d bytes", size)
            command |= BLOCK_SIZE_SPECIFIED
            struct.pack_into("<L", request, 4, size)
        else:
            logger.warning("Data size has not been specified")
        SDO_STRUCT.pack_into(request, 0, command, index, subindex)
        response = sdo_client.request_response(request)
        res_command, res_index, res_subindex = SDO_STRUCT.unpack_from(response)
        if res_command & 0xE0 != RESPONSE_BLOCK_DOWNLOAD:
            self.sdo_client.abort(0x05040001)
            raise SdoCommunicationError(
                "Unexpected response 0x%02X" % res_command)
        # Check that the message is for us
        if res_index != index or res_subindex != subindex:
            self.sdo_client.abort()
            raise SdoCommunicationError((
                "Node returned a value for 0x{:X}:{:d} instead, "
                "maybe there is another SDO client communicating "
                "on the same SDO channel?").format(res_index, res_subindex))
        self._blksize, = struct.unpack_from("B", response, 4)
        logger.debug("Server requested a block size of %d", self._blksize)
        self.crc_supported = bool(res_command & CRC_SUPPORTED)

    def write(self, b):
        """
        Write the given bytes-like object, b, to the SDO server, and return the
        number of bytes written. This will be at most 7 bytes.

        :param bytes b:
            Data to be transmitted.

        :returns:
            Number of bytes successfully sent or ``None`` if length of data is
            less than 7 bytes and the total size has not been reached yet.
        """
        if self._done:
            raise RuntimeError("All expected data has already been transmitted")
        # Can send up to 7 bytes at a time
        data = b[0:7]
        if self.size is not None and self.pos + len(data) >= self.size:
            # This is the last data to be transmitted based on expected size
            self.send(data, end=True)
        elif len(data) < 7:
            # We can't send less than 7 bytes in the middle of a transmission
            return None
        else:
            self.send(data)
        return len(data)

    def send(self, b, end=False):
        """Send up to 7 bytes of data.

        :param bytes b:
            0 - 7 bytes of data to transmit.
        :param bool end:
            If this is the last data.
        """
        assert len(b) <= 7, "Max 7 bytes can be sent"
        if not end:
            assert len(b) == 7, "Less than 7 bytes only allowed if last data"
        self._seqno += 1
        command = self._seqno
        if end:
            command |= NO_MORE_BLOCKS
            self._done = True
            # Change expected ACK:ed sequence
            self._blksize = self._seqno
            # Save how many bytes this message contains since this is the last
            self._last_bytes_sent = len(b)
        request = bytearray(8)
        request[0] = command
        request[1:len(b) + 1] = b
        self.sdo_client.send_request(request)
        self.pos += len(b)
        if self.crc_supported:
            # Calculate CRC
            self._crc = binascii.crc_hqx(b, self._crc)
        if self._seqno >= self._blksize:
            # End of this block, wait for ACK
            self._block_ack()

    def tell(self):
        return self.pos

    def _block_ack(self):
        logger.debug("Waiting for acknowledgement of last block...")
        response = self.sdo_client.read_response()
        res_command, ackseq, blksize = struct.unpack_from("BBB", response)
        if res_command & 0xE0 != RESPONSE_BLOCK_DOWNLOAD:
            self.sdo_client.abort(0x05040001)
            raise SdoCommunicationError(
                "Unexpected response 0x%02X" % res_command)
        if res_command & 0x3 != BLOCK_TRANSFER_RESPONSE:
            self.sdo_client.abort(0x05040001)
            raise SdoCommunicationError("Server did not respond with a "
                                        "block download response")
        if ackseq != self._blksize:
            self.sdo_client.abort(0x05040003)
            raise SdoCommunicationError(
                ("%d of %d sequences were received. "
                 "Retransmission is not supported yet.") % (ackseq, self._blksize))
        logger.debug("All %d sequences were received successfully", ackseq)
        logger.debug("Server requested a block size of %d", blksize)
        self._blksize = blksize
        self._seqno = 0

    def close(self):
        """Closes the stream."""
        if self.closed:
            return
        super(BlockDownloadStream, self).close()
        if not self._done:
            logger.error("Block transfer was not finished")
        command = REQUEST_BLOCK_DOWNLOAD | END_BLOCK_TRANSFER
        # Specify number of bytes in last message that did not contain data
        command |= (7 - self._last_bytes_sent) << 2
        request = bytearray(8)
        request[0] = command
        if self.crc_supported:
            # Add CRC
            struct.pack_into("<H", request, 1, self._crc)
        logger.debug("Ending block transfer...")
        response = self.sdo_client.request_response(request)
        res_command, = struct.unpack_from("B", response)
        if not res_command & END_BLOCK_TRANSFER:
            raise SdoCommunicationError("Block download unsuccessful")
        logger.info("Block download successful")

    def writable(self):
        return True


class SdoError(Exception):
    pass


class SdoAbortedError(SdoError):
    """SDO abort exception."""

    CODES = {
        0x05030000: "SDO toggle bit error",
        0x05040000: "Timeout of transfer communication detected",
        0x05040001: "Unknown SDO command specified",
        0x05040002: "Invalid block size",
        0x05040003: "Invalid sequence number",
        0x05040004: "CRC error",
        0x06010000: "Unsupported access to an object",
        0x06010001: "Attempt to read a write only object",
        0x06010002: "Attempt to write a read only object",
        0x06020000: "Object does not exist",
        0x06040042: "PDO length exceeded",
        0x06060000: "Access failed due to a hardware error",
        0x06070010: "Data type and length code do not match",
        0x06090011: "Subindex does not exist",
        0x06090030: "Value range of parameter exceeded",
        0x060A0023: "Resource not available",
        0x08000000: "General error",
        0x08000021: ("Data can not be transferred or stored to the application "
                     "because of local control"),
        0x08000022: ("Data can not be transferred or stored to the application "
                     "because of the present device state")
    }

    def __init__(self, code):
        #: Abort code
        self.code = code

    def __str__(self):
        text = "Code 0x{:08X}".format(self.code)
        if self.code in self.CODES:
            text = text + ", " + self.CODES[self.code]
        return text


class SdoCommunicationError(SdoError):
    """No or unexpected response from slave."""
