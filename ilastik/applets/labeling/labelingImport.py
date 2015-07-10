###############################################################################
#   ilastik: interactive learning and segmentation toolkit
#
#       Copyright (C) 2011-2015, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# In addition, as a special exception, the copyright holders of
# ilastik give you permission to combine ilastik with applets,
# workflows and plugins which are not covered under the GNU
# General Public License.
#
# See the LICENSE file for details. License information is also available
# on the ilastik web site at:
#		   http://ilastik.org/license.html
###############################################################################

#Python
import collections
import os
import numpy
import vigra

import logging
from lazyflow.operators import opArrayCache
logger = logging.getLogger(__name__)

#Qt
from PyQt4 import uic
from PyQt4.QtCore import Qt, QEvent
from PyQt4.QtGui import QDialog, QDialogButtonBox, QMessageBox, QCheckBox, QSpinBox, QLabel

# volumina
from volumina.utility import PreferencesManager

#lazyflow
import lazyflow
from lazyflow.roi import TinyVector, roiToSlice, roiFromShape
from lazyflow.operators.ioOperators import OpInputDataReader
from lazyflow.operators.opReorderAxes import OpReorderAxes
from lazyflow.operators.valueProviders import OpOutputProvider
from lazyflow.operators.opArrayCache import OpArrayCache
from lazyflow.operators.valueProviders import OpMetadataInjector

# ilastik
from ilastik.applets.dataSelection.dataSelectionGui import DataSelectionGui

def import_labeling_layer(labelLayer, labelingSlots, parent_widget=None):
    """
    Prompt the user for layer import settings, and perform the layer import.
    :param labelLayer: The top label layer source
    :param labelingSlots: An instance of LabelingGui.LabelingSlots
    :param parent_widget: The Qt GUI parent object
    """
    writeSeeds = labelingSlots.labelInput
    assert isinstance(writeSeeds, lazyflow.graph.Slot), "slot is of type %r" % (type(writeSeeds))
    opLabels = writeSeeds.getRealOperator()
    assert isinstance(opLabels, lazyflow.graph.Operator), "slot's operator is of type %r" % (type(opLabels))


    recentlyImported = PreferencesManager().get('labeling', 'recently imported')
    mostRecentProjectPath = PreferencesManager().get('shell', 'recently opened')
    mostRecentImageFile = PreferencesManager().get( 'DataSelection', 'recent image' )
    if recentlyImported:
        defaultDirectory = os.path.split(recentlyImported)[0]
    elif mostRecentProjectPath:
        defaultDirectory = os.path.split(mostRecentProjectPath)[0]
    elif mostRecentImageFile:
        defaultDirectory = os.path.split(mostRecentImageFile)[0]
    else:
        defaultDirectory = os.path.expanduser('~')

    fileNames = DataSelectionGui.getImageFileNamesToOpen(parent_widget, defaultDirectory)
    fileNames = map(str, fileNames)

    if not fileNames:
        return

    PreferencesManager().set('labeling', 'recently imported', fileNames[0])

    # Initialize operators
    opImport = OpInputDataReader( parent=opLabels.parent )
    opCache = OpArrayCache( parent=opLabels.parent )
    opMetadataInjector = OpMetadataInjector( parent=opLabels.parent )
    opReorderAxes = OpReorderAxes( parent=opLabels.parent )

    # Set up the pipeline as follows:
    #
    #   opImport --> opCache --> opMetadataInjector --------> opReorderAxes --(inject via setInSlot)--> labelInput
    #                           /                            /
    #   User-specified axisorder    labelInput.meta.axistags

    try:
        opImport.WorkingDirectory.setValue(defaultDirectory)
        opImport.FilePath.setValue(fileNames[0] if len(fileNames) == 1 else
                                   os.path.pathsep.join(fileNames))
        assert opImport.Output.ready()
    
        opCache.blockShape.setValue( opImport.Output.meta.shape )
        opCache.Input.connect( opImport.Output )
        assert opCache.Output.ready()
    
        # Load the data from file into our cache, and get the stats.
        readData = opCache.Output[:].wait()
        maxLabels = len(labelingSlots.labelNames.value)
        unique_read_labels, readLabelCounts = numpy.unique(readData, return_counts=True)
        labelInfo = (maxLabels, (unique_read_labels, readLabelCounts))
        del readData
    
        # Ask the user how to interpret the data.
        settingsDlg = LabelImportOptionsDlg( parent_widget,
                                             fileNames, opImport.Output,
                                             labelingSlots.labelInput, labelInfo )
        dlg_result = settingsDlg.exec_()
        if dlg_result != LabelImportOptionsDlg.Accepted:
            return
    
        imageOffsets = settingsDlg.imageOffsets
        labelMapping = settingsDlg.labelMapping
        updated_axisorder = str(settingsDlg.axesEdit.text())
    
        metadata = opCache.Output.meta.copy()
        metadata.axistags = vigra.defaultAxistags(updated_axisorder)
    
        # Change the interpretation of the file's axes
        opMetadataInjector.Input.connect( opCache.Output )
        opMetadataInjector.Metadata.setValue( metadata )
    
        # Transpose the axes for assignment to the labeling operator.
        opReorderAxes.AxisOrder.setValue( writeSeeds.meta.getAxisKeys() )
        opReorderAxes.Input.connect( opImport.Output )

        # Expect import is subset
        if (TinyVector(opReorderAxes.Output.meta.shape) > writeSeeds.meta.shape).any():
            QMessageBox.critical(parent_widget, "Import shape too large",
                                 "Import shape is not a subset of original input stack.")
            return

        # Expect x, y shape to match original shape
        labels_tagged_shape = labelingSlots.labelInput.meta.getTaggedShape()
        file_tagged_shape = opReorderAxes.Output.meta.getTaggedShape()
        if (  labels_tagged_shape['x'] != file_tagged_shape['x']
           or labels_tagged_shape['y'] != file_tagged_shape['y'] ):
            QMessageBox.critical(parent_widget, "Shape does not match",
                                 "X,Y shape must match original input stack.")
            return

        # Optimization if mapping is identity
        if labelMapping.keys() == labelMapping.values():
            labelMapping = None

        # Map input labels to output labels
        label_data = opReorderAxes.Output[:].wait()
        if labelMapping:
            # There are other ways to do a relabeling (e.g skimage.segmentation.relabel_sequential)
            # But this supports potentially huge values of unique_read_labels (in the billions),
            # without needing GB of RAM.
            mapping_indexes = numpy.searchsorted(unique_read_labels, label_data)
            new_labels = numpy.array([labelMapping[x] for x in unique_read_labels])
            label_data[:] = new_labels[mapping_indexes]

        label_roi = numpy.array( roiFromShape(opReorderAxes.Output.meta.shape) )
        label_roi += imageOffsets
        label_slice = roiToSlice(*label_roi)
        writeSeeds[label_slice] = label_data

    finally:
        opReorderAxes.cleanUp()
        opMetadataInjector.cleanUp()
        opCache.cleanUp()
        opImport.cleanUp()


#**************************************************************************
# LabelImportOptionsDlg
#**************************************************************************
class LabelImportOptionsDlg(QDialog):

    def __init__(self, parent, srcInputFiles, dataInputSlot, writeSeedsSlot, labelInfo):
        """
        Constructor.

        :param parent: The parent widget
        :param srcInputFiles: A list of source file names.
        :param dataInputSlot: Slot with imported data
        :param writeSeedsSlot: Slot for writing data into
        :param labelInfo: information about (max_labels, (read_labels, read_label_counts))
        """
        super( LabelImportOptionsDlg, self ).__init__(parent)

        localDir = os.path.split(__file__)[0]
        uic.loadUi( os.path.join( localDir, "dataImportOptionsDlg.ui" ), self)

        # TODO: 
        self._axisRanges = numpy.array(writeSeedsSlot.meta.shape) - dataInputSlot.meta.shape
        self._dataInputSlot = dataInputSlot
        self._srcInputFiles = srcInputFiles
        self._writeSeedsSlot = writeSeedsSlot
        self._labelInfo = labelInfo

        self._insert_position_boxes = collections.OrderedDict()
        self._insert_mapping_boxes = collections.OrderedDict()

        # Result values
        self.imageOffsets = LabelImportOptionsDlg._defaultImageOffsets(self._axisRanges, srcInputFiles, dataInputSlot)
        self.labelMapping = LabelImportOptionsDlg._defaultLabelMapping(labelInfo)

        # Init child widgets
        self._initMetaInfoWidgets()
        self._initInsertPositionMappingWidgets()

        # See self.eventFilter()
        self.installEventFilter(self)


    @staticmethod
    def _defaultImageOffsets(axisRanges, srcInputFiles, dataInputSlot):
        img_offset = [0] * len(axisRanges)

        # Note: Convenience setting of starting 'z' offset; assumes that filenames are
        # numbered from 0, and they contain only a single number representing their index
        if (srcInputFiles is not None):
            inputAxes = dataInputSlot.meta.getAxisKeys()
            z_idx = inputAxes.index('z')
            filename_digits = filter(str.isdigit, os.path.basename(srcInputFiles[0]))
            idx = int(filename_digits) if filename_digits else 0
            img_offset[z_idx] = max(0, min(idx, axisRanges[z_idx]))

        return img_offset

    @staticmethod
    def _defaultLabelMapping(labelInfo):
        # Note: Default mapping prefers mapping
        label_mapping = collections.defaultdict(int)

        max_labels, read_labels_info = labelInfo
        labels, label_counts = read_labels_info
        label_idx = max_labels;

        for i in reversed(labels):
            label_mapping[i] = label_idx if i > 0 else 0
            label_idx = max(0, label_idx - 1)

        return label_mapping


    def eventFilter(self, watched, event):
        # Ignore 'enter' keypress events, since the user may just be entering settings.
        # The user must manually click the 'OK' button to close the dialog.
        if watched == self and \
           event.type() == QEvent.KeyPress and \
           ( event.key() == Qt.Key_Enter or event.key() == Qt.Key_Return):
            return True
        return False


    #**************************************************************************
    # Input/Output Meta-info (display only)
    #**************************************************************************
    def _initMetaInfoWidgets(self):
        ## Input/output meta-info display widgets
        dataInputSlot = self._dataInputSlot
        writeSeedsSlot = self._writeSeedsSlot

        self.inputMetaInfoWidget.initSlot( dataInputSlot )
        self.labelMetaInfoWidget.initSlot( writeSeedsSlot )

        self._initSourceFilesList()


    def _initSourceFilesList(self):
        srcInputFiles = self._srcInputFiles
        map(self.inputFilesComboBox.addItem, map(os.path.basename, srcInputFiles))


    #**************************************************************************
    # Insertion Position / Mapping
    #**************************************************************************
    def _initInsertPositionMappingWidgets(self):
        inputAxes = self._dataInputSlot.meta.getAxisKeys()
        
        axisRanges = list(self._axisRanges)
        maxValues = axisRanges

        # Handle the 'c' axis separately
        c_idx = inputAxes.index('c')
        inputAxes_noC = inputAxes[:c_idx] + inputAxes[c_idx+1:]  # del(list(inputAxes)[c_idx])
        maxValues_noC = maxValues[:c_idx] + maxValues[c_idx+1:]  # del(list(maxValues)[c_idx])

        self._initInsertPositionTableWithExtents(inputAxes_noC, maxValues_noC)
        self._initLabelMappingTableWithExtents(maxValues[c_idx])

    def _initInsertPositionTableWithExtents(self, axes, mx):
        positionTbl = self.positionWidget

        tblHeaders = ["insert at", "max"]

        positionTbl.setColumnCount(len(tblHeaders))
        positionTbl.setHorizontalHeaderLabels(tblHeaders)
        positionTbl.resizeColumnsToContents()

        tagged_insert = collections.OrderedDict(zip(axes, self.imageOffsets))
        tagged_max = collections.OrderedDict(zip(axes, mx))
        self._tagged_insert = tagged_insert

        positionTbl.setRowCount(len(tagged_insert))
        positionTbl.setVerticalHeaderLabels(tagged_insert.keys())

        self._insert_position_boxes.clear()

        for row, (axis_key, extent) in enumerate(tagged_max.items()):
            # Init min/max spinboxes
            default_insert = tagged_insert[axis_key] or 0
            default_max = tagged_max[axis_key] or extent

            insertBox = QSpinBox(self)
            maxBox = QLabel(str(default_max), self)

            insertBox.setValue(0)
            insertBox.setMinimum(0)
            insertBox.setMaximum(extent)
            insertBox.setEnabled( tagged_insert[axis_key] is not None )
            if insertBox.isEnabled():
                insertBox.setValue( default_insert )

            # TODO: maxBox shouldn't be in tab list (but it still is)
            maxBox.setTextInteractionFlags(Qt.NoTextInteraction)
            maxBox.setFocusPolicy(Qt.NoFocus)
            maxBox.setEnabled(False)

            insertBox.valueChanged.connect( self._updatePosition )

            positionTbl.setCellWidget( row, 0, insertBox )
            positionTbl.setCellWidget( row, 1, maxBox )

            self._insert_position_boxes[axis_key] = (insertBox, maxBox)

        positionTbl.resizeColumnsToContents()

    def _initLabelMappingTableWithExtents(self, max_labels):
        mappingTbl = self.mappingWidget
        max_labels, read_labels_info = self._labelInfo
        labels, label_counts = read_labels_info
        label_mapping = self.labelMapping

        mappings = zip(labels, [label_mapping[i] for i in labels], label_counts)

        tblHeaders = ["map", "to", "px count"]
        mappingTbl.setColumnCount(len(tblHeaders))
        mappingTbl.setHorizontalHeaderLabels(tblHeaders)
        mappingTbl.resizeColumnsToContents()

        mappingTbl.setRowCount( len(labels) )
        mappingTbl.setVerticalHeaderLabels( map(lambda x: str(x), labels) )

        self._insert_mapping_boxes.clear()

        for row, (label_from, label_to, px_cnt) in enumerate(mappings):
            enabledBox = QCheckBox(self)
            mapToBox = QSpinBox(self)
            pxCountBox = QLabel(str(px_cnt), self)

            enabledBox.setChecked(label_to > 0)

            mapToBox.setMinimum(1 if label_to else 0)
            mapToBox.setMaximum(max_labels if label_to else 0)
            mapToBox.setValue(label_to)
            mapToBox.setEnabled(label_to > 0)

            enabledBox.stateChanged.connect( self._updateMappingEnabled )
            mapToBox.valueChanged.connect( self._updateMapping )

            # TODO: pxCountBox shouldn't be in tab list (but it still is)
            pxCountBox.setTextInteractionFlags(Qt.NoTextInteraction)
            pxCountBox.setFocusPolicy(Qt.NoFocus)
            pxCountBox.setEnabled(False)

            mappingTbl.setCellWidget( row, 0, enabledBox )
            mappingTbl.setCellWidget( row, 1, mapToBox )
            mappingTbl.setCellWidget( row, 2, pxCountBox )

            self._insert_mapping_boxes[label_from] = (enabledBox, mapToBox)

        mappingTbl.resizeColumnsToContents()


    #**************************************************************************
    # Update Position / Mapping
    #**************************************************************************
    def _updatePosition(self):
        inputAxes = self._dataInputSlot.meta.getAxisKeys()

        for (k,v) in self._insert_position_boxes.items():
            insertBox, _ = v
            self.imageOffsets[inputAxes.index(k)] = insertBox.value()

    def _updateMappingEnabled(self):
        max_labels, _ = self._labelInfo

        for (k,v) in self._insert_mapping_boxes.items():
            enabledBox, mapToBox = v
            enabled = enabledBox.isChecked()
            if enabled:
                label_to = mapToBox.value()
                label_to = min(max(1, k if not label_to else label_to), max_labels)
            else:
                label_to = 0

            self.labelMapping[k] = label_to

            mapToBox.setMinimum(1 if label_to else 0)
            mapToBox.setMaximum(max_labels if label_to else 0)
            mapToBox.setValue(label_to)
            mapToBox.setEnabled(label_to > 0)

        enabledBoxes, _ = zip(*self._insert_mapping_boxes.values())
        enableOk = any(map(QCheckBox.isChecked, enabledBoxes))

        self.buttonBox.button(QDialogButtonBox.Ok).setEnabled(enableOk)

    def _updateMapping(self):
        for (k,v) in self._insert_mapping_boxes.items():
            _, mapToBox = v
            self.labelMapping[k] = mapToBox.value()


