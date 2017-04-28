import logging
import time
import os
import sys
from ctypes import cdll
from services.parser.clang_parser import ClangParser

class TUnitPool():
    def __init__(self):
        self.tunits = {}

    def get(self, filename):
        return self.tunits.get(filename, None)

    def set(self, filename, tunit):
        self.tunits[filename] = tunit

    def drop(self, filename):
        if filename in self.tunits:
            del self.tunits[filename]

    def clear(self):
        self.tunits.clear()

    def __setitem__(self, key, item):
        self.tunits[key] = item

    def __getitem__(self, key):
        return self.tunits.get(key, None)

    def __iter__(self):
        return self.tunits.iteritems()

class ClangIndexer():
    def __init__(self, callback = None):
        self.parser = ClangParser()
        self.callback = callback
        self.indexer_directory_name = '.indexer'
        self.indexer_output_extension = '.ast'
        self.tunit_pool = TUnitPool()
        self.op = {
            0x0 : self.__run_on_single_file,
            0x1 : self.__run_on_directory,
            0x2 : self.__drop_single_file,
            0x3 : self.__drop_all,
            0x10 : self.__go_to_definition,
            0x11 : self.__find_all_references
        }

    def __call__(self, args):
        self.op.get(int(args[0]), self.__unknown_op)(int(args[0]), args[1:len(args)])

    def __unknown_op(self, id, args):
        logging.error("Unknown operation with ID={0} triggered! Valid operations are: {1}".format(id, self.op))

    def __load_single(self, tunit_filename, full_path):
        logging.info("Loading tunit {0} from {1}.".format(tunit_filename, full_path))
        try:
            self.tunit_pool[tunit_filename] = self.parser.load_tunit(full_path)
            #logging.info('TUnits load_from_disk() memory consumption (pympler) = ' + str(asizeof.asizeof(self.tunit_pool)))
        except:
            logging.error(sys.exc_info()[0])

    def __load_from_directory(self, indexer_directory):
        start = time.clock()
        self.tunit_pool.clear()
        for dirpath, dirs, files in os.walk(indexer_directory):
            for file in files:
                name, extension = os.path.splitext(file)
                if extension == self.indexer_output_extension:
                    tunit_filename = os.path.join(dirpath, file)[len(indexer_directory):-len(self.indexer_output_extension)]
                    logging.info("tunit_filename = {0}, file = {1}".format(tunit_filename, file))
                    self.__load_single(tunit_filename, os.path.join(dirpath, file))
        time_elapsed = time.clock() - start
        logging.info("Loading from {0} took {1}.".format(indexer_directory, time_elapsed))

    def __save_single(self, tunit, tunit_filename, dest_directory):
        tunit_full_path = os.path.join(dest_directory, tunit_filename[1:len(tunit_filename)])
        parent_dir = os.path.dirname(tunit_full_path)
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
        try:
            self.parser.save_tunit(tunit, tunit_full_path + self.indexer_output_extension)
        except:
            logging.error(sys.exc_info()[0])

    def __save_to_directory(self, indexer_directory):
        start = time.clock()
        #logging.info('TUnits memory consumption (pympler) = ' + str(asizeof.asizeof(self.tunit_pool)))
        for tunit_filename, tunit in self.tunit_pool:
            self.__save_single(tunit, tunit_filename, indexer_directory)
        time_elapsed = time.clock() - start
        logging.info("Saving to {0} took {1}.".format(indexer_directory, time_elapsed))

    def __index_single_file(self, proj_root_directory, contents_filename, original_filename, compiler_args):
        logging.info("Indexing a file '{0}' ... ".format(original_filename))

        # Append additional include path to the compiler args which points to the parent directory of current buffer.
        #   * This needs to be done because we will be doing analysis on temporary file which is located outside the project
        #     directory. By doing this, we might invalidate header includes for that particular file and therefore trigger
        #     unnecessary Clang parsing errors.
        #   * An alternative would be to generate tmp files in original location but that would pollute project directory and
        #     potentially would not play well with other tools (indexer, version control, etc.).
        if contents_filename != original_filename:
            compiler_args += ' -I' + os.path.dirname(original_filename)

        # TODO Indexing a single file does not guarantee us we'll have up-to-date AST's
        #       * Problem:
        #           * File we are indexing might be a header which is included in another translation unit
        #           * We would need a TU dependency tree to update influenced translation units as well

        # Index a single file
        start = time.clock()
        tunit = self.parser.run(contents_filename, original_filename, list(str(compiler_args).split()), proj_root_directory)
        time_elapsed = time.clock() - start
        logging.info("Indexing {0} took {1}.".format(original_filename, time_elapsed))

        return tunit

    def __run_on_single_file(self, id, args):
        proj_root_directory = str(args[0])
        contents_filename = str(args[1])
        original_filename = str(args[2])
        compiler_args = str(args[3])

        # Index a file
        tunit = self.__index_single_file(proj_root_directory, contents_filename, original_filename, compiler_args)

        if tunit is not None:
            if contents_filename == original_filename:
                # Serialize the indexing results to the disk
                self.__save_single(tunit, original_filename, os.path.join(proj_root_directory, self.indexer_directory_name))

                # Load indexing result from disk
                self.__load_single(
                    original_filename,
                    os.path.join(proj_root_directory, self.indexer_directory_name, original_filename) + self.indexer_output_extension
                )
            else:
                # We will skip AST serialization to the disk for temporary files.
                self.tunit_pool[original_filename] = tunit

        if self.callback:
            self.callback(id, args)

    def __run_on_directory(self, id, args):
        # NOTE  Indexer will index each file in directory in a way that it will:
        #           1. Index a file
        #           2. Flush its AST immediately to the disk
        #           3. Repeat 1 & 2 for each file
        #
        #       One might notice that 2nd step could have been:
        #           1. Run after whole directory has been indexed
        #              (which is possible because we keep all the translation units in memory)
        #           2. Skipped and executed on demand through a separate API (if and when client wants to)
        #
        #       Both approaches have been evaluated and it turned out that 'separate API' approach lead to
        #       very high RAM consumption (>10GB) which would eventually render the indexer non-functional
        #       for any mid- to large-size projects.
        #
        #       For example, running an indexer on a rather smallish project (cppcheck, ~330 files at this moment)
        #       would result in:
        #           1. RAM consumption of ~5GB if we would parse all of the files _AND_ flush the ASTs to the disk.
        #              The problem here is that RAM consumption would _NOT_ go any lower even after the ASTs have been
        #              flushed to disk which was strange enough ...
        #           2. RAM consumption of ~650MB if we would load all of the previously parsed ASTs from the disk.
        #       There is a big discrepency between these two numbers which clearly show that there is definitely some
        #       memory lost in the process.
        #
        #       Analysis of high RAM consumption has shown that issue was influenced by a lot of small object artifacts
        #       (small memory allocations), which are:
        #           1. Generated by the Clang-frontend while running its parser.
        #           2. Still laying around somewhere in memory even after parsing has been completed.
        #           3. Accumulating in size more and more the more files are parsed.
        #           4. Not a subject to memory leaks according to the Valgrind but rather flagged as 'still reachable' blocks.
        #           5. Still 'occupying' a process memory space even though they have been 'freed'.
        #               * It is a property of an OS memory allocator to decide whether it will or it will not swap this memory
        #                 out of the process back to the OS.
        #               * It does that in order to minimize the overhead/number of dynamic allocations that are potentially
        #                 to be made in near future and, hence, reuse already existing allocated memory chunk(s).
        #               * Memory allocator can be forced though to claim the memory back to the OS through
        #                 'malloc_trim()' call if supported by the OS, but this does not guarantee us to get to
        #                  the 'original' RAM consumption.
        #
        #       'Flush-immeditelly-after-parse' approach seems to not be having these issues and has a very low memory
        #       footprint even with the big-size projects.

        proj_root_directory = str(args[0])
        compiler_args = str(args[1])

        self.tunit_pool.clear()

        # TODO Run indexing of each file in separate (parallel) jobs to make it faster?
        indexer_directory_full_path = os.path.join(proj_root_directory, self.indexer_directory_name)
        if not os.path.exists(indexer_directory_full_path):
            logging.info("Starting to index whole directory '{0}' ... ".format(proj_root_directory))
            start = time.clock()
            for dirpath, dirs, files in os.walk(proj_root_directory):
                for file in files:
                    name, extension = os.path.splitext(file)
                    if extension in ['.cpp', '.cc', '.cxx', '.c', '.h', '.hh', '.hpp']:
                        filename = os.path.join(dirpath, file)
                        tunit = self.__index_single_file(proj_root_directory, filename, filename, compiler_args)
                        if tunit is not None:
                            # Serialize the indexing results to the disk
                            self.__save_single(tunit, filename, indexer_directory_full_path)

            time_elapsed = time.clock() - start
            logging.info("Indexing {0} took {1}.".format(proj_root_directory, time_elapsed))

        logging.info("Loading indexer results ... '{0}'.".format(indexer_directory_full_path))
        self.__load_from_directory(indexer_directory_full_path)

        if self.callback:
            self.callback(id, args)

    def __drop_single_file(self, id, args):
        self.tunit_pool.drop(str(args[0]))
        if self.callback:
            self.callback(id, args)

    def __drop_all(self, id, dummy = None):
        self.tunit_pool.clear()

        # Swap the freed' memory back to the OS. Parsing many translation units tend to
        # consume a big chunk of memory. In order to minimize the system memory footprint
        # we will try to swap it back.
        try:
            cdll.LoadLibrary("libc.so.6").malloc_trim(0)
        except:
            logging.error(sys.exc_info()[0])

        if self.callback:
            self.callback(id, dummy)

    def __go_to_definition(self, id, args):
        cursor = self.parser.get_definition(self.tunit_pool[str(args[0])], int(args[1]), int(args[2]))
        if cursor:
            logging.info('Definition location %s' % str(cursor.location))
        else:
            logging.info('No definition found.')

        if self.callback:
            self.callback(id, cursor.location if cursor else None)

    def __find_all_references(self, id, args):
        start = time.clock()
        references = self.parser.find_all_references(self.tunit_pool, self.tunit_pool[str(args[0])], int(args[1]), int(args[2]))
        time_elapsed = time.clock() - start
        logging.info("Find all references of [{0}, {1}] in {2} took {3}.".format(args[1], args[2], args[0], time_elapsed))
        for r in references:
            logging.info("Ref location %s" % str(r))

        if self.callback:
            self.callback(id, references)
