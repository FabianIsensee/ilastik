from lazyflow.graph import Graph, Operator, OperatorWrapper

from ilastik.workflow import Workflow

from ilastik.applets.projectMetadata import ProjectMetadataApplet
from ilastik.applets.dataSelection import DataSelectionApplet
from ilastik.applets.objectExtraction import ObjectExtractionApplet

from ilastik.applets.objectClassification import ObjectClassificationApplet

from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow.stype import Opaque
from lazyflow.operators.ioOperators.opInputDataReader import OpInputDataReader
from lazyflow.operators import OpAttributeSelector

class ObjectClassificationWorkflow( Workflow ):
    def __init__( self ):
        super(ObjectClassificationWorkflow, self).__init__()
        self._applets = []

        # Create a graph to be shared by all operators
        graph = Graph()
        self._graph = graph
        ######################
        # Interactive workflow
        ######################
        
        ## Create applets 
        self.projectMetadataApplet = ProjectMetadataApplet()
        self.dataSelectionApplet = DataSelectionApplet(graph, "Input Segmentation", "Input Segmentation", batchDataGui=False)
        self.objectExtractionApplet = ObjectExtractionApplet( graph )
        self.objectClassificationApplet = ObjectClassificationApplet( graph )
        
        ## Access applet operators
        opData = self.dataSelectionApplet.topLevelOperator
        opObjExtraction = self.objectExtractionApplet.topLevelOperator
        opObjClassification = self.objectClassificationApplet.topLevelOperator
        
        opObjExtraction.BinaryImage.connect( opData.Image )

        opObjClassification.BinaryImages.connect( opData.Image )
        opObjClassification.InputImages.connect( opObjExtraction.LabelImage )
        opObjClassification.ObjectFeatures.connect( opObjExtraction.RegionFeatures )        
        opObjClassification.LabelsAllowedFlags.connect( opData.AllowLabels )
        #opObjClassification.MaxObjectNumber.connect( opObjExtraction.RegionCount )
        
        self._applets.append(self.projectMetadataApplet)
        self._applets.append(self.dataSelectionApplet)
        self._applets.append(self.objectExtractionApplet)
        self._applets.append(self.objectClassificationApplet)
        
        # The shell needs a slot from which he can read the list of image names to switch between.
        # Use an OpAttributeSelector to create a slot containing just the filename from the OpDataSelection's DatasetInfo slot.
        opSelectFilename = OperatorWrapper( OpAttributeSelector, graph=graph )
        opSelectFilename.InputObject.connect( opData.Dataset )
        opSelectFilename.AttributeName.setValue( 'filePath' )

        self._imageNameListSlot = opSelectFilename.Result
        
    @property
    def applets(self):
        return self._applets
    

    @property
    def imageNameListSlot(self):
        return self._imageNameListSlot
    
    @property
    def graph( self ):
        '''the lazyflow graph shared by the applets'''
        return self._graph